# NapGuard Known Limitations

1. **Restricted workload support.** Only NapGuard's verified reference workloads `c-single` and `c-threads` can be launched.
   - Python programs are refused: their restores crashed in Stage 02.
   - Programs holding sockets, GUI programs, multi-process programs, programs with devices open, static binaries, and setuid binaries are all refused.
   - Arbitrary Linux processes are not supported, and NapGuard never attaches to processes it did not launch.
2. **Developer-only coordinator security.** DMTCP 4.2.0's coordinator has no authentication. NapGuard confines it to loopback, audits listeners, and refuses multi-user hosts, but any local user could still talk to it. That is why:
   - the daemon starts coordinators only after an explicit, per-run developer acknowledgement;
   - the systemd unit runs in refused mode;
   - NapGuard is not production-authorized ([ADR 0006](decisions/0006-stage03-dmtcp-backend-integration-and-coordinator-security.md), [ADR 0008](decisions/0008-mvp-developer-demo-integration.md)).
3. **External files are not rolled back.** A restore brings back the process's memory, not the files or other state it changed after the checkpoint (LIM-007).
4. **Only the newest checkpoint is restorable.** DMTCP overwrites an instance's image at each checkpoint, so older generations are history only (SUPERSEDED). A restored instance is not checkpointed again in this version.
5. **VM battery.** A UTM VM exposes no battery, so battery support is reported as unavailable. Battery events in the Simulation Lab are simulated and labelled; the checkpoints they cause are real.
6. **No suspend/resume integration.** NapGuard does not hook system sleep, and does nothing while the system is suspended.
7. **The daemon owns its workloads.** Stopping the daemon stops its demo workloads. A restarted daemon marks earlier running instances LOST.
8. **No cold-boot auto-recovery, retention, or quotas.**
9. **ARM64 only verified.** DMTCP 4.2.0 on Ubuntu 24.04 aarch64 is the only verified platform.
10. **The GUI is not tested on the development Mac**, which has no Tk. It is checked on Ubuntu with `napguard gui --smoke` inside the final verification script.

## Troubleshooting

| Symptom | Cause | What to do |
|---|---|---|
| Banner: "daemon is not reachable" | The daemon is not running, or a different socket is in use | Press **Start daemon (developer mode)**, or run `napguard daemon --i-accept-single-user-coordinator` |
| `PERMISSION_DENIED` when launching | The daemon is in refused mode, or the host does not look single-user, or an audit failed | Restart the daemon in developer mode; log out other users; check the Activity log for "exposure" |
| `BACKEND_UNAVAILABLE` | DMTCP 4.2.0 is not built from the pinned commit | `bash experiments/stage02/build-dmtcp.sh` |
| `VERIFY_FAILED` on restore | The restored process could not be proven to be this restore's own and continuous | View Technical Details (it names the PID and the reason); make sure no stray demo process is running; try again |
| Restore button disabled (✖) | The checkpoint is not the newest PUBLISHED one of a stopped workload | Stop the workload; restore the newest generation |
| Checkpoint INVALID | The manifest or image changed after capture | Delete it; it will never be restored |
| `CONFLICT` on archive, delete, or reset | Something is running or an operation is queued | Stop the workloads and wait for the operation to finish |
| GUI does not start | Tkinter is missing, or there is no display | `sudo apt install python3-tk`; run from the desktop session |
| "Battery: Unavailable" | The VM has no battery hardware | Expected; use the Simulation Lab |
