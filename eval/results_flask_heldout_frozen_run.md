# Kyun evaluation on pallets/flask

- Questions: 14
- Kyun correct: **78.6%**
- Flat search baseline correct (same data, no graph): **38.5%** (Kyun on the same subset: 76.9%)
- Refused unanswerable questions: 2/2 (baseline answered 2/2 of them anyway)
- Overridden decisions flagged: 0/0 (baseline quoted the overridden one in 0/0)
- Median answer time: 2 ms

| # | Category | Question | Kyun | Kyun status | Baseline |
|---|---|---|---|---|---|
| H01 | rationale | Why does redirect return 303 instead of 302? | ✅ | answered (0.86) | ✅ right evidence |
| H02 | rationale | Why is should_ignore_error deprecated? | ✅ | answered (0.83) | ❌ wrong evidence |
| H03 | rationale | Why was FLASK_ENV deprecated? | ✅ | answered (0.73) | ✅ right evidence |
| H04 | rationale | Why is before_first_request deprecated? | ✅ | answered (0.79) | ❌ wrong evidence |
| H05 | reversal | Why was from flask import Markup restored? | ✅ | what_not_why (0.44) | ✅ right evidence |
| H06 | reversal | Why was Config.from_json re-added? | ❌ right evidence | answered (0.65) | ❌ wrong evidence |
| H07 | reversal | Why is the URL matched after the session is loaded? | ✅ | answered (0.85) | ✅ right evidence |
| H08 | no reason recorded | Why does Flask use flit_core instead of setuptools? | ✅ | what_not_why (0.43) | ✅ right evidence |
| H09 | dependency | Why are signals always available now? | ❌ right evidence | what_not_why (0.35) | ❌ wrong evidence |
| H10 | refusal | Why does Flask use Kubernetes for deployment? | ✅ | not_in_graph (0.0) | ❌ returned an answer anyway |
| H11 | refusal | Why was the GraphQL blueprint removed? | ✅ | not_in_graph (0.0) | ❌ returned an answer anyway |
| H12 | who to ask | Who should I ask about app.py? | ✅ | answered (0.8) | n/a |
| H13 | lifecycle | Why is __version__ deprecated? | ✅ | answered (0.81) | ❌ wrong evidence |
| H14 | rationale | Why are json_encoder and json_decoder only passed to dumps when they are custom? | ❌ status, right evidence | what_not_why (0.37) | ❌ wrong evidence |
