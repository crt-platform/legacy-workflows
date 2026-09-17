#!/usr/bin/env python3
"""Unattended microservice deploy — the replacement for Heimdall's deploy flow.

Runs on the self-hosted `ms` runner, which can reach the create-dev boxes over
the create-shared <-> create-dev peering. Config is rendered ON THE BOX from its
instance role, so no AWS credentials exist here.

Modes:
  plan    connect and verify only; changes nothing (default)
  stage   everything up to and including `npm ci`, but never reloads pm2
  deploy  the real thing, with a health gate and rollback to last-good

Per host the sequence mirrors Heimdall step for step; see
crt-agents/ci-cd/heimdall-deprecation.md section 5.
"""

import argparse
import json
import os
import shlex
import subprocess
import sys
import time

import yaml

HASH_DIR = "/deployment/.heimdall/package-lock-hashes"  # shared with Heimdall on purpose
STATE_DIR = "/deployment/.deploy"
SETTINGS_RE = r'^app.*\.settings\.js$'


class Fail(Exception):
    pass


def log(msg):
    print(msg, flush=True)


class Box:
    """One target host."""

    def __init__(self, name, ip, user, key, service, root):
        self.name, self.ip, self.user, self.key = name, ip, user, key
        self.service, self.root = service, root
        self.cwd = f"{root}/{service}"

    def _ssh_base(self):
        return [
            "ssh", "-i", self.key,
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=10",
            f"{self.user}@{self.ip}",
        ]

    def run(self, command, cwd=True, check=True, timeout=900):
        """Always a LOGIN shell: nvm puts node/npm/pm2 on PATH via ~/.bashrc."""
        prefix = f"cd {shlex.quote(self.cwd)} && " if cwd else ""
        inner = (
            "source ~/.bashrc >/dev/null 2>&1 || true; "
            "source ~/.profile >/dev/null 2>&1 || true; "
            f"{prefix}{command}"
        )
        proc = subprocess.run(
            self._ssh_base() + ["bash", "-lc", shlex.quote(inner)],
            capture_output=True, text=True, timeout=timeout,
        )
        if check and proc.returncode != 0:
            raise Fail(f"[{self.name}] command failed ({proc.returncode}): {command}\n"
                       f"{proc.stdout.strip()}\n{proc.stderr.strip()}")
        return proc.stdout.strip()

    def push(self, local, remote):
        subprocess.run(
            ["scp", "-i", self.key, "-o", "BatchMode=yes",
             "-o", "StrictHostKeyChecking=accept-new", "-r", local,
             f"{self.user}@{self.ip}:{remote}"],
            check=True, capture_output=True, text=True,
        )


def pm2_apps(box, cfg):
    """The pm2 processes this deployment directory owns.

    Usually one, named after the service. `erp.wms` is the exception: a single
    /deployment/erp.wms with ONE dist/ and three processes told apart by an
    APP_KEY env (SKL/THM/CRT), each reading its own app_<KEY>.settings.js.
    They cannot be deployed separately — replacing dist/ replaces the code all
    three run — so they are reloaded and verified together. Reloading only one
    would leave the others running old code from memory over a new dist, to be
    picked up untested at the next restart.
    """
    return list(cfg.get("pm2_apps") or [box.service])


def pm2_process(box, name=None):
    raw = box.run("pm2 jlist 2>/dev/null", cwd=False)
    start = raw.find("[")
    if start < 0:
        raise Fail(f"[{box.name}] pm2 jlist returned no JSON")
    want = name or box.service
    for proc in json.loads(raw[start:]):
        if proc.get("name") == want:
            return proc
    return None


def health_port(cfg, app):
    """`port` for a single-process service, or `ports: {app: port}` for several."""
    h = cfg.get("health") or {}
    return h["ports"].get(app) if "ports" in h else h.get("port")


def health(box, cfg, app=None):
    """HTTP status as a string, or None when this app has no health endpoint.

    `health` is optional: erp.logistic and erp.quartup answer on no path we could
    find, and a service whose gate can never pass is worse than a weaker gate.
    """
    port = health_port(cfg, app or box.service)
    if not cfg.get("health") or not port:
        return None
    url = f"http://127.0.0.1:{port}{cfg['health']['path']}"
    code = box.run(f"curl -s -o /dev/null -w '%{{http_code}}' --max-time 10 {shlex.quote(url)}",
                   cwd=False, check=False)
    return code.strip()[-3:]


def ensure_clone(box, owner):
    exists = box.run(
        f"git -C {shlex.quote(box.cwd)} rev-parse --is-inside-work-tree 2>/dev/null && echo YES || echo NO",
        cwd=False, check=False)
    if "YES" in exists:
        return "already cloned"
    box.run(f"mkdir -p {shlex.quote(box.root)}", cwd=False)
    box.run(f"git clone git@github.com:{owner}/{box.service} {shlex.quote(box.cwd)}", cwd=False)
    return "cloned"


def checkout(box, version):
    box.run("git fetch --tags --force")
    box.run("git reset --hard")
    box.run(f"git checkout {shlex.quote(version)}")


def unpack_artifact(box, local_dist_gz):
    """Snapshot the current dist/ before overwriting it.

    Rollback CANNOT rely on `git checkout <previous>`: dist/ is build output and
    is not tracked by git, so a checkout alone leaves the new, broken bundle in
    place and pm2 reloads exactly the code that just failed. Keeping the previous
    dist on the box also makes rollback independent of GitHub's 90-day artifact
    retention.
    """
    box.run(f"mkdir -p {shlex.quote(box.root)}/artifacts/{box.service} {STATE_DIR}", cwd=False)
    box.run(f"if [ -d dist ]; then rm -rf {STATE_DIR}/{box.service}.dist.prev && "
            f"cp -a dist {STATE_DIR}/{box.service}.dist.prev; fi")
    remote = f"{box.root}/artifacts/{box.service}/dist.gz"
    box.push(local_dist_gz, remote)
    box.run(f"tar -xzf {shlex.quote(remote)}")


def restore_previous_dist(box):
    snap = f"{STATE_DIR}/{box.service}.dist.prev"
    present = box.run(f"[ -d {snap} ] && echo YES || echo NO", cwd=False, check=False)
    if "YES" not in present:
        raise Fail(f"[{box.name}] no previous dist snapshot at {snap} — cannot roll back the build output")
    box.run(f"rm -rf dist && cp -a {snap} dist")
    return "restored previous dist"


def render_config(box, cfg_dir_local, secret_id, region, renderer_local):
    tmp = f"{box.root}/tmp_configurations/{box.service}"
    box.run(f"rm -rf {shlex.quote(tmp)} && mkdir -p {shlex.quote(tmp)}", cwd=False)
    for entry in sorted(os.listdir(cfg_dir_local)):
        box.push(os.path.join(cfg_dir_local, entry), f"{tmp}/{entry}")
    box.push(renderer_local, f"{tmp}/.render-config.js")
    out = box.run(
        f"node {shlex.quote(tmp)}/.render-config.js "
        f"{shlex.quote(secret_id)} {shlex.quote(region)} {shlex.quote(tmp)} {shlex.quote(box.cwd)}",
        cwd=False)
    box.run(f"rm -rf {shlex.quote(tmp)}", cwd=False)
    return out


def npm_install(box, force):
    """Heimdall's normalized-lock-hash skip, reusing its hash file so the two
    systems never disagree about whether install is needed."""
    current = box.run(
        "if [ ! -f package-lock.json ]; then echo __NO_LOCK__; else "
        "node -e \"const fs=require('fs'),c=require('crypto');"
        "const l=JSON.parse(fs.readFileSync('package-lock.json','utf8'));"
        "delete l.version; if(l.packages&&l.packages['']){delete l.packages[''].version;}"
        "process.stdout.write(c.createHash('sha256').update(JSON.stringify(l)).digest('hex'))\" "
        "2>/dev/null || echo __NO_LOCK__; fi", check=False)
    stored = box.run(f"[ -f {HASH_DIR}/{box.service}.sha256 ] && "
                     f"tr -d '\\r\\n\\t ' < {HASH_DIR}/{box.service}.sha256 || echo __NONE__",
                     cwd=False, check=False)
    if not force and len(current) == 64 and current == stored:
        return "skipped (package-lock unchanged)"
    box.run(
        "mkdir -p /tmp/.deploy_bin && printf '#!/bin/sh\\nexit 0\\n' > /tmp/.deploy_bin/husky && "
        "chmod +x /tmp/.deploy_bin/husky && "
        "PATH=/tmp/.deploy_bin:$PATH HUSKY=0 npm ci --prefer-offline --no-audit "
        "--progress=false --omit=dev --loglevel=error --no-update-notifier; "
        "rc=$?; rm -rf /tmp/.deploy_bin; exit $rc", timeout=1800)
    if len(current) == 64:
        box.run(f"mkdir -p {HASH_DIR} && printf '%s' {shlex.quote(current)} > "
                f"{HASH_DIR}/{box.service}.sha256", cwd=False)
    return "npm ci ran"


def reload_and_verify(box, cfg, settle):
    """Reload the ecosystem file, then verify EVERY pm2 app it owns.

    `pm2 reload ecosystem.config.js` already restarts every app declared there,
    so a multi-process service is reloaded as a unit whether we like it or not —
    the gate therefore has to judge all of them. One unhealthy vertical fails the
    deploy for all three, which is correct: they share a dist/.
    """
    apps = pm2_apps(box, cfg)
    baseline = {}
    for app in apps:
        proc = pm2_process(box, app)
        baseline[app] = proc["pm2_env"]["restart_time"] if proc else 0

    box.run(" && ".join(f"pm2 flush {shlex.quote(a)}" for a in apps) +
            f" && pm2 reload {shlex.quote(box.cwd)}/ecosystem.config.js --time && pm2 reset all",
            cwd=False)
    log(f"  settling {settle}s before judging health")
    time.sleep(settle)

    first = {}
    for app in apps:
        proc = pm2_process(box, app)
        if proc is None:
            raise Fail(f"[{box.name}] {app} is not in pm2 after reload")
        first[app] = proc["pm2_env"]["restart_time"]
    codes = {app: health(box, cfg, app) for app in apps}   # None = no endpoint declared
    time.sleep(5)

    notes = []
    for app in apps:
        # pm2 reports a crash-looping process as `online`, so status is never the
        # test: a restart counter that keeps moving is.
        second = pm2_process(box, app)["pm2_env"]["restart_time"]
        if second != first[app]:
            raise Fail(f"[{box.name}] {app}: restart counter still climbing "
                       f"({baseline[app]} -> {first[app]} -> {second}): crash loop")
        code = codes[app]
        if code is None:
            # The restart check still catches a crash loop, but nothing here proves
            # the app can answer: one that boots and serves errors passes.
            notes.append(f"{app} restarts stable at {second} (NO health check)")
        elif code != "200":
            raise Fail(f"[{box.name}] {app}: health {cfg['health']['path']} "
                       f"(port {health_port(cfg, app)}) returned {code}, expected 200")
        else:
            notes.append(f"{app} health 200, restarts stable at {second}")

    box.run("pm2 save", cwd=False)
    return "; ".join(notes)


def last_good(box):
    return box.run(f"[ -f {STATE_DIR}/{box.service}.last-good ] && "
                   f"cat {STATE_DIR}/{box.service}.last-good || echo ''",
                   cwd=False, check=False).strip()


def set_last_good(box, version):
    box.run(f"mkdir -p {STATE_DIR} && printf '%s' {shlex.quote(version)} > "
            f"{STATE_DIR}/{box.service}.last-good", cwd=False)


def deploy_host(box, cfg, env, args, paths):
    results = []
    log(f"\n=== {box.name} ({box.ip})")

    apps = pm2_apps(box, cfg)
    for app in apps:
        proc = pm2_process(box, app)
        if proc is None:
            raise Fail(f"[{box.name}] {app} has no pm2 process here — wrong `hosts` or "
                       f"`pm2_apps` in {box.service}/deploy/{args.environment}.yaml? "
                       f"(a settings file is not proof of deployment)")
        log(f"  pm2 {app}: status={proc['pm2_env']['status']} "
            f"restarts={proc['pm2_env']['restart_time']} "
            f"health={health(box, cfg, app) or 'n/a'}")
    log(f"  last-good: {last_good(box) or '(none recorded)'}")

    if args.mode == "plan":
        secret_id = env["secret_path"].format(service=box.service)
        ok = box.run(f"aws secretsmanager describe-secret --secret-id {shlex.quote(secret_id)} "
                     f"--region {env['region']} --query Name --output text 2>&1 | tail -1",
                     cwd=False, check=False)
        log(f"  secret readable as {box.user}: {ok}")
        gate = (f"{cfg['health']['path']} on {', '.join(f'{a}:{health_port(cfg, a)}' for a in apps)}"
                if cfg.get("health") else "restart counter only (no health block)")
        log(f"  WOULD: checkout {args.version}, unpack artifact, render "
            f"{len(os.listdir(paths['config_dir']))} config file(s), npm ci if lock changed, "
            f"pm2 reload {len(apps)} app(s), verify {gate}")
        return ["plan only"]

    results.append(ensure_clone(box, env["artifact_owner"]))
    checkout(box, args.version)
    results.append(f"checked out {args.version}")
    unpack_artifact(box, paths["dist_gz"])
    results.append("artifact unpacked")
    results.append(render_config(box, paths["config_dir"],
                                 env["secret_path"].format(service=box.service),
                                 env["region"], paths["renderer"]))
    if cfg.get("clear_cache") or args.clear_cache:
        box.run("rm -rf cache")
        results.append("cache cleared")
    results.append(npm_install(box, cfg.get("force_npm_ci") or args.force_npm_ci))

    if args.mode == "stage":
        results.append("STOPPED before pm2 reload (stage mode) — box holds new files, old process")
        return results

    previous = last_good(box)
    try:
        results.append(reload_and_verify(box, cfg, env.get("health_settle_seconds", 20)))
        set_last_good(box, args.version)
    except Fail as exc:
        log(f"  !! {exc}")
        if not previous or previous == args.version:
            raise Fail(f"[{box.name}] health gate failed and there is no different last-good "
                       f"version to fall back to (recorded: {previous or 'none'})")
        log(f"  rolling back to {previous}")
        checkout(box, previous)
        log("  " + restore_previous_dist(box))
        npm_install(box, force=True)
        results.append(reload_and_verify(box, cfg, env.get("health_settle_seconds", 20)))
        raise Fail(f"[{box.name}] deploy of {args.version} failed health; rolled back to {previous}")
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--service", required=True)
    ap.add_argument("--version", required=True)
    ap.add_argument("--environment", default="crt_dev")
    ap.add_argument("--mode", choices=["plan", "stage", "deploy"], default="plan")
    ap.add_argument("--hosts", default="", help="comma list overriding the service YAML")
    ap.add_argument("--ms-deploy", required=True, help="path to the ms-deploy checkout")
    ap.add_argument("--artifact", default="", help="path to dist.gz (not needed for plan)")
    ap.add_argument("--force-npm-ci", action="store_true")
    ap.add_argument("--clear-cache", action="store_true")
    args = ap.parse_args()

    key = os.environ.get("SSH_KEY_PATH")
    if not key or not os.path.isfile(key):
        raise Fail("SSH_KEY_PATH is unset or missing")

    env_file = os.path.join(args.ms_deploy, "deploy", "environments.yaml")
    with open(env_file) as fh:
        environments = yaml.safe_load(fh)
    if args.environment not in environments:
        raise Fail(f"unknown environment '{args.environment}' in {env_file}")
    env = environments[args.environment]

    svc_file = os.path.join(args.ms_deploy, args.service, "deploy", f"{args.environment}.yaml")
    if not os.path.isfile(svc_file):
        # Data-gated rollout: no file means this service is still Heimdall's.
        log(f"NOT_ENROLLED: {args.service} has no deploy/{args.environment}.yaml — skipping")
        return 0
    with open(svc_file) as fh:
        cfg = yaml.safe_load(fh)
    if "hosts" not in cfg:
        raise Fail(f"{svc_file} is missing required key 'hosts'")
    if cfg.get("health"):
        h = cfg["health"]
        if not h.get("path"):
            raise Fail(f"{svc_file} has a health block without 'path'")
        if not h.get("port") and not h.get("ports"):
            raise Fail(f"{svc_file} health needs 'port', or 'ports' mapping each pm2 app to one")
        if h.get("ports"):
            missing = [a for a in (cfg.get("pm2_apps") or [args.service]) if a not in h["ports"]]
            if missing:
                raise Fail(f"{svc_file} health.ports is missing {missing} — every pm2 app needs a port")

    # `enabled: false` keeps a service's config ready without ever deploying it.
    # The sqlserver ETLs are the case: deliberately stopped in dev, and a
    # `pm2 reload` of the ecosystem file would START them again.
    if cfg.get("enabled", True) is False:
        log(f"DISABLED: {args.service} has enabled:false in deploy/{args.environment}.yaml — skipping")
        return 0

    host_names = [h.strip() for h in args.hosts.split(",") if h.strip()] or cfg["hosts"]
    unknown = [h for h in host_names if h not in env["hosts"]]
    if unknown:
        raise Fail(f"unknown host(s) {unknown}; known: {sorted(env['hosts'])}")

    config_dir = os.path.join(args.ms_deploy,
                              env["config_path"].format(service=args.service))
    if not os.path.isdir(config_dir):
        raise Fail(f"no config directory {config_dir}")
    if args.mode != "plan" and not args.artifact:
        raise Fail("--artifact is required for stage/deploy")

    paths = {
        "config_dir": config_dir,
        "dist_gz": args.artifact,
        "renderer": os.path.join(os.path.dirname(os.path.abspath(__file__)), "render-config.js"),
    }

    log(f"service={args.service} version={args.version} env={args.environment} "
        f"mode={args.mode} hosts={host_names}")

    summary = []
    for name in host_names:                       # sequential on purpose: never both boxes at once
        box = Box(name, env["hosts"][name], env["ssh_user"], key,
                  args.service, env.get("deployment_root", "/deployment"))
        summary.append((name, deploy_host(box, cfg, env, args, paths)))

    log("\n=== summary")
    for name, steps in summary:
        log(f"  {name}: " + "; ".join(steps))

    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a") as fh:
            fh.write(f"### {args.service} `{args.version}` ({args.mode})\n\n")
            for name, steps in summary:
                fh.write(f"- **{name}** — " + "; ".join(steps) + "\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Fail as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        sys.exit(1)
