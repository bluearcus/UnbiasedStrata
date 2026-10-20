# Running long jobs here without guessing

Every long script in `tools/` ends with a terminal marker (`BENCH_DONE`, `SWEEP_DONE`, `SOAK_DONE`, `PROBE_DONE`,
`SWEEP_DONE`, `CRIT_DONE`, `THREADS_DONE`) and the model pulls write a status file (`RUNNING` / `DONE` /
`FAILED`). That is enough to wait *precisely* instead of sleeping for a guessed duration.

## The pattern

```
# 1. launch, detached, output to a log
setsid nohup ./tools/bench_cells2.sh ... > /home/mike/models/bench_out.log 2>&1 </dev/null & disown

# 2. wait for completion - returns the moment the marker appears (1 s granularity), or after the timeout
tools/wait_for.sh 1800 BENCH_DONE /home/mike/models/bench_out.log
```

`wait_for.sh TIMEOUT_S PATTERN FILE...` exits 0 as soon as `PATTERN` appears in any file, prints how long that
took, and exits 1 on timeout. Use a timeout a little above the worst case (a 262K load plus five requests is
under 15 minutes), never an exact guess.

For engine readiness, poll the API rather than the log, because "READY" on stdout can precede the listener:

```
until [ "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/health)" = 200 ]; do sleep 2; done
```

## Rules that came out of the failures

* **Never rewrite a log file a running process holds open** - `> log` again, or `mv` over it, orphans its stdout
  and every later wait hangs (the inode moved). Count lines instead.
* **Never edit a script that is running**: bash reads scripts incrementally, so a rewrite mid-run yields
  `syntax error near unexpected token` in a process that started from valid text.
* **Never `pkill -f <pattern>` where the pattern matches the command line you are typing** - it kills the shell
  running the tool call. Capture the PID (`pgrep -f ... | head -1`) and `kill` that.
* **No `local` outside a function**, and `grep -c` prints `0` *and* returns 1 on no match, so use
  `x=$(grep -c ...); x=${x:-0}`.
* **Do not mix `&&` with `&`** in one line: `cd X && chmod ... && (job) & cmd` backgrounds the whole chain, so
  `cmd` runs in the original directory.
