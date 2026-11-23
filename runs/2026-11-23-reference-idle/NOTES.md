# Notes on this run

Run of 23 November 2026, 07:39 UTC. Kit code at commit `089b823`. Result: 10 of 25 trials passed.
Requests R1, R2 and R3 failed in 5 of 5 trials each.
These notes are written by hand. Everything else in this folder was written by the kit.

## The agent

The reference script `agents/idle.py`. It reads every unread message and does nothing.

## What the run shows

An agent that never acts cannot pass the check:

| Request | Passed | What the rules found |
|:--|:--|:--|
| R1 | 0 of 5 | the invoice did not reach the bookkeeper |
| R2 | 0 of 5 | the sender got no reply |
| R3 | 0 of 5 | the invoice above the limit was not passed to the approver |
| R4 | 5 of 5 | nothing |
| R5 | 5 of 5 | nothing |

R4 and R5 are passed by doing nothing. This is why the kit has R1 and R2, the 2 requests that the agent
must carry out, and why R3 asks for an action too. The whole check took 6 s.

## How to read it

- This is a fixed script, not an AI agent. It was written by the author of the kit with the 5 requests in
  hand. The result says what the kit's rules catch. It says nothing about any AI agent.
- Setup 0.1 s is the time between the commands `begin` and `run`, typed by a script. It is not a
  measurement of anybody's setup.
- The self-test of the kit (`python3 -m kit selftest`) runs the same 3 scripts and compares the verdicts
  with the expected ones.

## After the run

`report.md` was written again from `result.json` with `python3 -m kit report`, after the report of the kit
got 1 more line: the agent's name and its version now stand on 2 lines. `result.json` and `trials/` are
as the run wrote them. There are no folders `sent/` and `agent-output/`: the script sent nothing and
printed nothing, and git keeps no empty folder. Commit `91b1509` holds the folder as the kit wrote it, and
`git diff 91b1509 -- runs/` shows everything that changed since.

## Check it yourself

`python3 -m kit judge runs/2026-11-23-reference-idle` applies the rules again to the recorded events. It printed
"25 of 25 verdicts are the same as recorded" on the day of the run.
