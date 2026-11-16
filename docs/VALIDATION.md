# Validation · version 1.7.5

Evidence dated **2026-11-16**. All fixture identities, policyholder consent, human requests and emails are simulated. The six starter fixtures are unchanged.

## Current results

| Check | Result | Evidence |
| --- | --- | --- |
| Automated suite (offline, no API calls) | **372 passed**, 0 failed, 0 skipped; one dependency deprecation warning | `test-results.xml` |
| Lint / format | `ruff check` and `ruff format --check` clean (60 files) | — |
| Real-model end-to-end matrix (gpt-4.1-mini, 1.7.5 Docker image, fresh volume) | **17/17 cases, 148/148 checks** | `e2e-results.json` (full transcripts) |
| Invalid API key | Included in the matrix (`modelfailure`): gates enforced, graceful scripted replies | `e2e-results.json` |
| Docker | Image built and run on Docker Engine 29.6 (Linux x86_64): `/healthz`, page load, real-model chat, volume persistence across `docker restart`, logout | this page |
| API contracts | Regenerated for 1.7.5 (version only; `national_id_last4` added in 1.7.4) | `contracts/` |
| 1.7.5 fixes, real model on the deployed image | Consent pushback explained; second refusal names Request human help; third refusal stops asking; one empathy sentence per reply; Margaret happy path 9/9; `/healthz` reports `1.7.5` | this page |

The e2e matrix (`evals/e2e_matrix.py`) drives the HTTP API exactly as the web UI does, one cookie jar per simulated browser. Its cases:

| Case | What it checks |
| --- | --- |
| `happy` | Spec test case in one utterance: verified, hint memory selects CL-2048, grounded denial reason, amounts, summary, send |
| `angry` | Empathy first, why verification matters, allowed fields, SSN refusal → alternatives, verify with email + DOB, deferred question answered |
| `scope` | "What is RL?" ×3 politely declined, human offered, no claim implied before verification; prompt injection unverified and verified |
| `preverify` | "Yes or no, was it denied?", "How many claims do I have?", claim-ID echo probe: nothing disclosed |
| `mismatch` | Mixed identities and 4 right + 1 wrong: "don't match our records", no field hint, no "policy number", no "provide three" |
| `lockout` | Three failures lock; correct data refused while locked; new session in the same browser still locked |
| `otherparty` | Verified Margaret asks for Ava / POL-1044 and CL-3001: refused, still verified as Margaret, own claims still work |
| `ambiguity` | Two January healthcare claims listed; "how much for that claim?" re-asks; CL-2011 (780), CL-2102 (3,200), CL-2048 (1,450 / 0.00) |
| `documents` | Submission, scan/format, review time, receipt: grounded, no invented timeframes; Ma Tian CL-3001 diagnosis report → default guidance |
| `representative` | David approved path, Ava refused, Margaret's earlier H-requests invisible, actor named in next steps; consent timeout → H-reference; unregistered "Ava's husband" refused |
| `identity` | Alias "Yaven Li" + alias email, phone `(650) 521-2836` with DOB "March 15, 1985", `650-521-2836`, Ma Tian national ID |
| `human` | Async H-reference, wait-only acknowledgment, back to AI, AI won't approve a late appeal, every message answered |
| `post` | Closing intent → summary, other-address request refused, send then cancel, skip |
| `long` | 30-turn conversation without budget or validation errors |
| `chinese` | Chinese identity + question: verified, CL-2048 resolved, grounded answer, PII redacted in the stored transcript |
| `tabs` | New session keeps identity; logout signs out every tab and locks the account endpoint |
| `modelfailure` | Server with an invalid key: no disclosure, wrong data rejected, correct data verifies, grounded fallback answer |

## 1.7.5 changes

| Issue | Fix | Regression tests (`test_revision_v175.py`) |
| --- | --- | --- |
| Representative pushback while consent is pending got only the pending template | Server text: acknowledgment, why consent is needed (private information; protects the policyholder even from family), Check policyholder consent / Request human help | `test_consent_pushback_is_acknowledged_explained_and_actionable`, `test_plain_consent_question_without_pushback_keeps_pending_template` |
| Second verification refusal said only "other ways to get help" | Names Request human help and the H-reference | `test_second_refusal_names_the_human_request_option` |
| Third refusal could still list fields / SSN alternatives (found while testing) | The turn that locks persuasion returns the human-request text only | `test_third_refusal_stops_persuading_even_on_an_ssn_refusal` |
| Two empathy sentences stacked (server + model) | `limit_empathy` keeps the first acknowledgment, never content | `test_limit_empathy_*`, `test_model_empathy_is_not_stacked_on_server_empathy` |
| `/healthz` version could drift from the deployed code | Single `app.__version__` for `/healthz`, `/v1/config`, OpenAPI | `test_health_and_config_report_the_package_version` |

## Regression tests for the submission bug list

| Bug | Fix | Regression tests |
| --- | --- | --- |
| P0 budget / validation | Grounded template on rejected wording; scripted parser + visible note when the model is unreachable, the evaluation budget is spent or the per-conversation ceiling (`MODEL_MAX_CALLS_PER_CONVERSATION`, default 300) is hit; amounts render from the fixture | `test_submission_bugs.py::test_conversation_call_limit_*`, `test_amount_answer_failure_shows_grounded_amounts`, `test_summary_failure_uses_grounded_template`; `test_live_budget.py`; `test_forms_and_answers.py::test_ungrounded_or_unreviewed_answer_is_not_shown` |
| P0 other person's policy | Server refusal keeps the grant; representative mode exitable | `test_other_party_request_is_refused_and_verification_kept`, `test_representative_mode_is_exited_by_the_policyholder`, `test_approved_representative_can_switch_to_policyholder` |
| P1 lockout | Failures counted per browser owner for 30 minutes | `test_lockout_is_cumulative_across_sessions_and_subject_changes`, `test_unverified_subject_change_keeps_transcript_and_failure_count` |
| P1 mismatch wording | Server-only mismatch text; validator blocks claim IDs, implied claims and "policy number" as an ID field before verification; form hint + phone placeholder | `test_mismatch_reply_is_server_text_without_field_hint`, `test_unverified_replies_cannot_echo_claims_or_list_policy_number`, `test_identity_request_before_verification_must_say_why` |
| P1 implied claim | Unverified off-topic reply rewritten | `test_offtopic_before_verification_does_not_imply_a_claim` |
| P1 ambiguity | Server candidate list, re-ask, question carried to the chosen claim; claim IDs never become policy numbers; denial question narrows to the denied claim | `test_ambiguous_claims_are_listed_by_server_and_reasked`, `test_denial_question_narrows_ambiguous_claims_to_the_denied_one` |
| Minor actor wording | Representative next steps name the actor in chat and email | `test_representative_next_steps_name_the_actor`, `test_revision_v171.py::test_summary_attributes_question_to_representative` |
| Found during testing | Different-address request no longer revokes identity; "You cannot…" no longer read as "you can…"; dates/amounts written by the server count as authorized | `test_summary_cannot_be_redirected_and_identity_is_untouched`, `test_validator_does_not_read_cannot_as_can` |

## Spend

Real-model testing for 1.7.4 and 1.7.5 used an estimated **US$2.45** at gpt-4.1-mini list price ($0.40 / $1.60 per million input/output tokens): about $1.99 for 1.7.4, then for 1.7.5 the full matrix ($0.34), the production happy path ($0.01) and about 37 probe turns (≈ $0.10, estimated from per-turn averages). That is within the $5 authorization. Estimates are not billing statements.

## Historical reports

Reports from versions ≤ 1.7.3 are in [`history/`](history/README.md), kept for traceability. They are not reruns of 1.7.4. Host addresses and local ledger paths in them were replaced with placeholders (`demo-host`, `<local-ledger-dir>`). The 1.7.3 PDF export of the technical guide is there too; the Markdown guide is current. Finite tests cannot establish correctness for every utterance.
