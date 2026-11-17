SPORE PROVIDER FAILURE RECOVERY (November 17, 2026)

What changes
  * Historical passes that contain ONLY confirmed Sail Research ttft_deadline_guard
    retries are annotated VOID_PROVIDER in attempt_reviews. Original attempts and
    raw logs are preserved. Verified matches and genuine failures remain intact.
  * New provider-only failures are logged to provider_incidents and do not use a pass.
  * OpenCode session ID is reused per function across passes, including retries.
    CLI still uses a private process/server each pass for pinned ephemeral provider
    config; this is conversation reuse, NOT one permanent OpenCode server.
  * New logs use unique filenames and never overwrite previous audit evidence.
  * A SQLite inference semaphore limits simultaneous provider calls independently
    from worker process count. Default: at most 8 simultaneous inferences.
  * Preview-first installer backs up replaced Python scripts. Recovery --apply
    makes an online SQLite database backup BEFORE any schema mutation.
  * spore_matched_bytes.py and runner status display VOID_PROVIDER separately.

Install (AFTER stopping every worker):

    cd ~/spore-decomp
    unzip -qo /mnt/c/Users/patrick/Downloads/spore_provider_recovery_fix.zip -d .
    .venv/bin/python install_spore_provider_fix.py
    .venv/bin/python install_spore_provider_fix.py --apply

Recovery preview and apply (no API usage):

    .venv/bin/python spore_recover_provider_attempts.py --show 30
    .venv/bin/python spore_recover_provider_attempts.py --apply
    .venv/bin/python spore_parallel_sqlite.py status
    .venv/bin/python spore_matched_bytes.py

Conservative recovery: historical rows with missing/overwritten logs, provider
errors with any model/tool activity, or no explicit ttft_deadline_guard
are LEFT ALONE and counted under "Unverified rows" in the preview.

Resume example (32 logical workers, at most 8 OpenRouter inferences at once):

    .venv/bin/python spore_parallel_sqlite.py run \
       --workers 32 --max-inflight 8 --passes 3 --reasoning high \
       --minutes 5 --quota-wait 1 --provider-retries 2 \
       --budget 20 --unsafe

If Sail Research still triggers the guard, lower --max-inflight to 5 or 4.
Start with a short batch using --max-functions-per-worker 1 if preferred.

Safety
  * No historical attempt rows are deleted by recovery.
  * Only rows with log evidence matching their recorded timestamp are voided.
  * Real compiler mismatches still use a pass.
  * The provider timeout still means NO model output, so no pass is spent.
  * Notes/history in SQLite and C++ source files are retained.
  * A stopped session can later be resumed by OpenCode session ID, if available.
  * If OpenCode's version rejects --session or --title, the error stops the worker
    without spending a pass. Check `opencode run --help` to verify options.
  * A max-inflight cap is NOT a provider rate-limit guarantee.

Known limitations
  * One OpenCode process/private server is still started per pass. Reusing the
    session saves the conversation and should reduce title requests, but not
    the full process startup overhead.
  * Attempt histories whose original logs were overwritten remain unresolved,
    because no safe automatic evidence exists for them.
