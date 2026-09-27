#!/usr/bin/env python3
"""Regenerate seccomp-userns.json from Docker's default seccomp profile.

Codex sandboxes each request with bwrap, which needs to create user, mount and
network namespaces. Docker's default profile denies all of them, so bwrap fails
with "No permissions to create a new namespace" and no request can run. This
permits the namespace syscalls and leaves the rest of the profile alone.

    curl -fsSL -o default.json \
      https://raw.githubusercontent.com/moby/moby/v27.0.3/profiles/seccomp/default.json
    python3 gen-seccomp.py default.json seccomp-userns.json

Re-run it when the Docker daemon is upgraded, using that release's tag.
"""
import json
import sys

# CLONE_NEWNS|NEWCGROUP|NEWUTS|NEWIPC|NEWUSER|NEWPID|NEWNET. The default profile
# allows clone() only when every one of these bits is zero.
BLOCK_ALL = 2114060288

# bwrap needs the whole set, not a subset: Codex enforces `network.enabled =
# false` by unsharing a *network* namespace, so keeping CLONE_NEWNET denied
# fails exactly as if no namespace were permitted at all.
PERMIT = BLOCK_ALL
STILL_BLOCKED = BLOCK_ALL & ~PERMIT


def main() -> None:
    src, dst = sys.argv[1], sys.argv[2]
    profile = json.load(open(src))

    patched = 0
    for rule in profile["syscalls"]:
        if rule.get("names") == ["clone"] and rule.get("args"):
            for arg in rule["args"]:
                if arg.get("value") == BLOCK_ALL:
                    arg["value"] = STILL_BLOCKED
                    patched += 1
    if patched != 2:
        raise SystemExit(f"expected 2 clone rules to patch, found {patched}; "
                         "the upstream profile changed shape, review it by hand")

    # Only reachable inside the new user namespace, where the process holds
    # CAP_SYS_ADMIN over that namespace and nothing else. clone3 is left as the
    # upstream ENOSYS rule so glibc keeps falling back to clone().
    profile["syscalls"].append({
        "names": ["unshare", "mount", "umount2", "pivot_root"],
        "action": "SCMP_ACT_ALLOW",
    })

    # Rules gated behind includes.caps never fire, because the container holds
    # no capabilities; only unconditional allows widen the sandbox.
    unconditional = {
        name for rule in profile["syscalls"]
        if rule.get("action") == "SCMP_ACT_ALLOW" and not rule.get("includes")
        for name in rule.get("names", [])
    }
    for denied in ("init_module", "finit_module", "bpf", "perf_event_open",
                   "kexec_load", "open_by_handle_at", "process_vm_writev"):
        if denied in unconditional:
            raise SystemExit(f"{denied} became unconditionally allowed; profile is too loose")

    json.dump(profile, open(dst, "w"), indent=2)
    print(f"patched {patched} clone rules, {len(profile['syscalls'])} syscall rules total")
    print("still denied: init_module, finit_module, bpf, perf_event_open, "
          "kexec_load, open_by_handle_at, process_vm_writev")


if __name__ == "__main__":
    main()
