Built a little AI agent this weekend 🤖

Point it at any public GitHub repo and it clones it, reads through the code, and spits back a plain-English explainer — what it does, how it's built, how to run it.

Tested it on Flask and it actually picked up on real architectural details (like the sans-IO layer that lets Quart run async on top of it) — not just surface-level summary fluff.

Code's open source if you want to try it: github.com/jhaveribhavishi/repo-explainer

[attach video]
