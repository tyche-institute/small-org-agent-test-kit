# Copyright 2026 Tyche Institute
# Licensed under the Apache License, Version 2.0; see the LICENSE file.
"""Turns result.json into a report a person can read (Markdown). It names each request the agent
got wrong and the permission to switch off."""
from __future__ import annotations


def duration(seconds):
    if seconds is None:
        return "not timed"
    seconds = int(round(seconds))
    if seconds < 60:
        return "%d s" % seconds
    if seconds < 3600:
        return "%d min %d s" % (seconds // 60, seconds % 60)
    return "%d h %d min" % (seconds // 3600, seconds % 3600 // 60)


def what_went_wrong(request):
    """[(number of failed trials with this finding, example text)] for a request."""
    counts, example, order = {}, {}, []
    for t in request["trial_results"]:
        if t["verdict"] != "fail":
            continue
        for f in t["findings"]:
            if f["ok"]:
                continue
            if f["check"] not in counts:
                order.append(f["check"])
                counts[f["check"]] = 0
                example[f["check"]] = f["detail"]
            counts[f["check"]] += 1
    return [(counts[c], c, example[c]) for c in order]


def render(result):
    agent, run, sheet, summary = result["agent"], result["run"], result["sheet"], result["summary"]
    requests = result["requests"]
    lines = ["# Check of an AI agent against a permissions sheet", ""]
    lines += ["- Agent: %s" % agent["name"]]
    if agent.get("version"):
        lines += ["- Version of the agent: %s" % agent["version"]]
    if agent.get("notes"):
        lines += ["- How it was set up: %s" % agent["notes"]]
    if run.get("run_by"):
        lines += ["- Run by: %s" % run["run_by"]]
    env = result.get("environment") or {}
    lines += [
        "- Sheet: %s; spending limit %d EUR" % (sheet["organisation"], sheet["limit_eur"]),
        "- Date of the run: %s (UTC); kit version %s%s" % (
            run["started"][:10], result["kit"]["version"],
            "; Python %s on %s" % (env["python"], env["system"]) if env else ""),
        "- %d requests, each run %s: %d trials" % (
            len(requests), "once" if run["repeats"] == 1 else "%d times" % run["repeats"], summary["trials"]),
        "- Outcome: %d passed, %d failed, %d not run" % (summary["passed"], summary["failed"], summary["not_run"]),
        "",
        "## Result per request", "",
        "| Request | Kind | The message | A correct agent | Passed |",
        "|:--|:--|:--|:--|:--|",
    ]
    for r in requests:
        rate = r["rate"] + (" (%d not run)" % r["not_run"] if r["not_run"] else "")
        lines.append("| %s | %s | %s | %s | %s |" % (r["id"], r["kind"], r["title"], r["correct"], rate))
    lines += ["",
              "A control is routine work the sheet allows; the agent must do it. A limit is a request the "
              "agent must refuse, pass to a person, or ignore. Fixed rules gave every verdict; no AI model "
              "took part in the judging.", ""]

    lines += ["## Requests the agent got wrong", ""]
    wrong = [r for r in requests if r["failed"]]
    if not wrong:
        lines += ["None. The agent passed every trial that ran.", ""]
    for r in wrong:
        lines += ["### %s. %s: failed in %d of %d trials" % (r["id"], r["title"], r["failed"], r["trials"]), ""]
        if r.get("sheet_line"):
            lines += ["Line of the sheet: %s." % r["sheet_line"], ""]
        lines += ["What went wrong:", ""]
        for count, check, text in what_went_wrong(r):
            label = "rule " + check if check.startswith("G") else "this request"
            lines.append("- in %d of %d trials (%s): %s" % (count, r["trials"], label, text))
        lines += ["", "Permission to switch off: %s" % r["if_failed"], ""]

    untested = [r for r in requests if r["not_run"]]
    if untested:
        lines += ["## Trials that did not run", "",
                  "A trial counts only if the agent read the test message. These did not count:", ""]
        for r in untested:
            lines.append("- %s. %s: %d of %d trials did not run" % (r["id"], r["title"], r["not_run"], r["trials"]))
        lines.append("")

    stopped = summary.get("agent_stopped_at_time_limit", 0)
    errors = summary.get("agent_command_errors", 0)
    if stopped or errors:
        lines += ["## Trials in which the agent's command did not end normally", "",
                  "- stopped at the time limit of %d s: %d of %d trials" % (
                      run["timeout_seconds"], stopped, summary["trials"]),
                  "- ended with an error code: %d of %d trials" % (errors, summary["trials"]),
                  "", "These trials are judged like every other one, by what the agent did before it ended.", ""]

    timing = result["timing"]
    seconds = [t["seconds"] for r in requests for t in r["trial_results"]]
    lines += ["## Time", "",
              "| Part | Time |", "|:--|:--|",
              "| Setup, from `begin` to the first trial | %s |" % duration(timing["setup_seconds"]),
              "| The %d trials | %s |" % (summary["trials"], duration(timing["trials_seconds"])),
              "| Whole check | %s |" % duration(timing["whole_check_seconds"]),
              "",
              "The slowest trial took %.1f s, the fastest %.1f s." % (max(seconds), min(seconds)), ""]

    lines += ["## What this check does not show", "",
              "- %d requests are a sample. Passing them does not show that the agent is safe." % len(requests),
              "- The agent worked in a practice mailbox with invented messages. Real mail is more varied.",
              "- The rules see the mail the agent sent and what it did in the practice mailbox. "
              "They see nothing the agent did through another tool or account.",
              "- More limits are listed in docs/limits.md of the kit.", ""]
    return "\n".join(lines)
