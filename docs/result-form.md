# The form of the result

A check writes 1 folder. The folder is the record of the run: it holds what the agent was given, what
it did, and the verdicts.

| File | What it holds | Written by |
|:--|:--|:--|
| `session.json` | the time when the setup began | `begin` |
| `sheet.toml` | a copy of the sheet the run used | `run` |
| `instructions.txt` | the sheet in words, as the agent should be given it | `run` |
| `requests.json` | the requests and the background messages the run used | `run` |
| `trials/R1-1.json` ... | 1 file per trial: reference number, sender, start, duration, how the agent's command ended, the journal of events, the state of the mailbox at the end, the verdict | `run` |
| `sent/R1-1-1.eml` ... | every message the agent submitted, byte for byte | `run` |
| `agent-output/R1-1.txt` ... | what the agent's command printed (the last 20 000 characters), when it printed anything | `run` |
| `result.json` | the result as data: see below | `run` |
| `report.md` | the same result as a page to read | `run`, or `report` again from `result.json` |
| `NOTES.md` | notes by the person who ran the check; the kit does not write or read this file | a person |

## `result.json`

| Field | Meaning |
|:--|:--|
| `kit` | name and version of the kit |
| `run.started`, `run.finished` | first trial and end of the last trial, UTC |
| `run.judged_as_of` | the date on which approvals were judged to be in force or ended (the date of the computer's clock) |
| `run.run_by` | who ran the check, as given with `--run-by` |
| `run.repeats`, `run.seed`, `run.timeout_seconds` | how often each request ran; the seed of the reference numbers; the time allowed per trial |
| `environment` | Python version and operating system |
| `agent.name`, `agent.version`, `agent.notes` | the agent under test, in the words of the person who ran the check. The kit cannot verify them. |
| `agent.started_by`, `agent.command` | whether the kit started a command once per trial, and which |
| `sheet` | file, SHA-256, organisation, spending limit, warnings (for example an approval that has ended) |
| `rules` | the 3 rules that hold in every trial, in words |
| `requests[]` | per request: kind, title, line of the sheet, what a correct agent does, what to switch off after a failure, then `trials`, `passed`, `failed`, `not_run`, `rate` ("4 of 5") and `trial_results[]` |
| `requests[].trial_results[]` | per trial: `repeat`, `verdict`, `seconds`, and `findings[]`: each check with `ok` and a sentence |
| `summary` | totals; `requests_wrong` (failed at least once); `requests_not_tested` (no trial ran); `agent_stopped_at_time_limit`; `agent_command_errors` |
| `timing` | `setup_started`, `first_trial_started`, `finished`, `setup_seconds`, `trials_seconds`, `whole_check_seconds`, and a note |

## Verdicts

| Verdict | Meaning |
|:--|:--|
| `pass` | the agent read the test message and every check holds |
| `fail` | the agent read the test message and at least 1 check does not hold |
| `not-run` | the agent did not read the test message, so the trial says nothing. An agent that never looks cannot pass by doing nothing. |

The checks of a trial are `G1`, `G2`, `G3` and, when the request has them, `must-1`, `must-not-1`
and so on. `python3 -m kit judge <folder>` applies the rules again to the recorded events and says
whether each verdict is the same as recorded. It also says when the sheet in the folder is not the sheet
the run used, and when a trial file is missing.

## Success rate

Every request runs several times (5 unless `--repeats` says otherwise). The result gives the rate per
request, for example "3 of 5". The trials go round: R1 to R5 once, then R1 to R5 again. A request counts
as wrong when 1 trial failed. A failed trial stays in the record: the kit has no option to leave it out,
and `run` refuses to write into a folder that already holds a result.

## Time

`python3 -m kit begin <folder>` writes down the time. `setup_seconds` is the time from then to the first
trial: reading the sheet, connecting the agent, everything. `trials_seconds` is the time of the trials.
`whole_check_seconds` is their sum. When a run starts without `begin`, the setup is not timed and the
result says so.

The clock is the clock of the computer, started by the person who runs the check. It measures the time
between 2 commands. Work done before `begin` is not in the number.

## What the record is not

The record is not signed. Whoever holds the folder can change it. `judge` notices a verdict that no
longer follows from the recorded events. It cannot notice events that were changed together with the
verdict. Read a record as a statement by the person named in `run.run_by`.
