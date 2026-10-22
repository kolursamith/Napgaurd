# NapGuard Teacher Demonstration (5–7 minutes)

Goal: show that NapGuard saves a process's **real execution state** with DMTCP, terminates it, restores it, and proves it **resumed** instead of starting again. Then show automation, simulation, and safe cleanup.

## Before the class (5 minutes, once)

On the Ubuntu VM desktop, from the repository:

```bash
git fetch origin && git checkout stage-03/backend-integration && git pull --ff-only
bash scripts/install.sh --with-desktop
```

`python3-tk` must be installed (`sudo apt install python3-tk`, your decision), and DMTCP 4.2.0 built (`bash experiments/stage02/build-dmtcp.sh`). Open **NapGuard** from the application grid. If old records exist, stop any running workload, then use **Settings & Help > Reset Demo…** and confirm.

## Script

| Time | Do | Say |
|---|---|---|
| 0:00 | Open **NapGuard**. If the banner shows "Daemon not running", press **Start daemon (developer mode)** and accept. | "NapGuard runs a background daemon, only as my user. This developer build talks to the DMTCP checkpoint engine, which is safe only on a single-user machine, so I confirm that explicitly." |
| 0:45 | **Dashboard**: point at the cards. | "The daemon and DMTCP 4.2.0 are ready. No battery: this is a VM, and NapGuard says so instead of inventing one." |
| 1:15 | **Guided Demo > Run Next Step** (Step 1), then Step 2. | "It launches a test program that counts in memory, with a random nonce as its identity. The counter is advancing." |
| 2:00 | Step 3 (**Create checkpoint**). | "DMTCP has saved the whole process: memory, registers, threads. NapGuard verified the checkpoint is complete, not just that a command returned 0." |
| 2:30 | Step 4 (**Stop original**). | "The original process is now killed and confirmed gone." |
| 3:00 | Step 5 (**Verify checkpoint**). | "Integrity check: the manifest's fingerprint from the database, then every file's SHA-256." |
| 3:20 | Step 6 (**Restore**), then Step 7 (**Verify recovery**). Point at the evidence table. | "DMTCP brought the process back. Same nonce, so it is the same process state, not a new start. The counter went back to the checkpoint value and keeps counting. NapGuard also checks the restored process is really ours." |
| 4:30 | **Processes**: the workload is **RESTORED**. Hover a disabled **Checkpoint Now**. | "Every button explains when it is allowed. Re-checkpointing a restored process is deliberately not supported yet." |
| 5:00 | **Simulation Lab**: Processes > Launch Demo Process first, then **Trigger Simulated Low Battery**. | "The battery reading is SIMULATED and labelled. But the checkpoint it triggered is real; here is its result in the timeline." |
| 5:45 | **Automation**: enable, 60 s, Save. Optionally **Checkpoint All Eligible Now**. | "The scheduler checkpoints running workloads automatically, never two operations at once." |
| 6:15 | **Activity**: filter **Error** and **Warning**; open one event's technical details. | "Everything is logged; simulated events are marked." |
| 6:40 | Stop the workloads, then **Settings & Help > Reset Demo…** and show the preview. | "Cleanup previews exactly what will be deleted, never touches running work, and keeps the audit log." |

## If something fails during the demo

- The guided demo **stops at the failed step**. It never skips an integrity or ownership check, and the dialog shows the error code and a suggested action.
- **VERIFY_FAILED** on restore: open **View Technical Details**, then use **Restart Demo** to run it again.
- **Daemon not running**: use the banner button.
- More in [Known Limitations](KNOWN_LIMITATIONS.md#troubleshooting).

## Closing message

> "NapGuard restores the genuine execution state of supported processes, verifies continuity instead of trusting exit codes, and is explicit about its limits: a developer build, reference workloads only, and no rollback of external files."
