# Kyun evaluation on psf/requests

- Questions: 21
- Kyun correct: **100.0%**
- Flat search baseline correct (same data, no graph): **26.3%** (Kyun on the same subset: 100.0%)
- Refused unanswerable questions: 3/3 (baseline answered 3/3 of them anyway)
- Overridden decisions flagged: 4/4 (baseline quoted the overridden one in 1/4)
- Median answer time: 1 ms

| # | Category | Question | Kyun | Kyun status | Baseline |
|---|---|---|---|---|---|
| Q01 | supersession | Why doesn't requests cache a default SSLContext? | ✅ | answered (0.95) | ✅ right decision |
| Q02 | supersession (docs trap) | Does requests reuse a global SSLContext when verify is True? | ✅ | answered (0.97) | ❌ quoted the overridden decision |
| Q03 | rationale | Why does requests use charset_normalizer instead of chardet? | ✅ | answered (0.83) | ❌ wrong evidence |
| Q04 | rationale + rename | Why is get_connection deprecated? | ✅ | answered (0.99) | ✅ right decision |
| Q05 | incident | What changed in the code because of CVE-2024-35195? | ✅ | answered (1.0) | ❌ wrong evidence |
| Q06 | rationale | Why is the requests[security] extra a no-op? | ✅ | answered (0.45) | ❌ wrong evidence |
| Q07 | incident | Why was proxy credential handling changed in 2.31.0? | ✅ | answered (0.79) | ❌ wrong evidence |
| Q08 | rationale | Why does pinning for chardet and idna use the major version? | ✅ | answered (0.86) | ✅ right decision |
| Q09 | supersession | Why was the default User-Agent changed? | ✅ | answered (0.85) | ❌ wrong evidence |
| Q10 | no reason recorded | Why does the chardet upper limit keep increasing? | ✅ | what_not_why (0.44) | ❌ wrong evidence |
| Q11 | no reason recorded | Why is DEFAULT_POOLSIZE 10? | ✅ | what_not_why (0.4) | ❌ wrong evidence |
| Q12 | false premise | Why is the retry limit three? | ✅ | what_not_why (0.4) | ❌ wrong evidence |
| Q13 | entity lifecycle | Why was get_os_ca_bundle_path removed? | ✅ | what_not_why (0.4) | ❌ wrong evidence |
| Q14 | revert | Why was 'Use urllib for chunked requests' reverted? | ✅ | what_not_why (0.25) | ✅ right decision |
| Q15 | revert | Why was restricting URL preparation to HTTP/HTTPS reverted? | ✅ | what_not_why (0.36) | ✅ right decision |
| Q16 | no reason recorded | Why did requests drop Python 2.7? | ✅ | what_not_why (0.26) | ❌ wrong evidence |
| Q17 | refusal | Why does requests not support HTTP/2? | ✅ | not_in_graph (0.0) | ❌ returned an answer anyway |
| Q18 | refusal | Why was GraphQL support removed? | ✅ | not_in_graph (0.0) | ❌ returned an answer anyway |
| Q19 | refusal | Why does requests use Rust for parsing? | ✅ | not_in_graph (0.0) | ❌ returned an answer anyway |
| Q20 | who to ask | Who should I ask about adapters.py? | ✅ | answered (0.8) | n/a |
| Q21 | who to ask | Who knows the cookie handling code? | ✅ | answered (0.8) | n/a |
