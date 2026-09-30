# Changelog

## 1.0.0 (2026-09-11)


### Features

* automated releases with release-please ([#40](https://github.com/Simi24/ralph-gh/issues/40)) ([937d502](https://github.com/Simi24/ralph-gh/commit/937d50257a8a06c13d14912d573cea4394c75c69))
* gate review depth scales with the sensitivity of the touched code ([#35](https://github.com/Simi24/ralph-gh/issues/35)) ([56b03f3](https://github.com/Simi24/ralph-gh/commit/56b03f3bd261066c24bbe3cc718d263409092826))
* graceful stop — finish the in-flight iteration, always write the summary, release cleanly ([#28](https://github.com/Simi24/ralph-gh/issues/28)) ([39fc283](https://github.com/Simi24/ralph-gh/commit/39fc2834f3d8f47a0e1b609b735ff107b02171e7))
* initial release — GitHub-issue-driven Ralph loop with deterministic review gates ([2f83f4f](https://github.com/Simi24/ralph-gh/commit/2f83f4f635d9f91e27de457bb14e4471d7cfc125))
* install drift check and documented update path ([#41](https://github.com/Simi24/ralph-gh/issues/41)) ([71afa77](https://github.com/Simi24/ralph-gh/commit/71afa7733480bfc9bed846ed7abf4225de6663fc)), closes [#39](https://github.com/Simi24/ralph-gh/issues/39)
* observability — phase visibility on GitHub and timestamped logs ([#34](https://github.com/Simi24/ralph-gh/issues/34)) ([34200f3](https://github.com/Simi24/ralph-gh/commit/34200f3e8ec7d50f22827947171e8e3e55addb91))
* ship the /ralph-gh wrapper skill and install it alongside agents ([6be1a0b](https://github.com/Simi24/ralph-gh/commit/6be1a0b4257aa6a5ffdfc61c308043c9ae5d4e4b))
* usage-limit awareness — pause and requeue instead of failing the issue ([#31](https://github.com/Simi24/ralph-gh/issues/31)) ([cf88bfa](https://github.com/Simi24/ralph-gh/commit/cf88bfa8e9c8c32942d862be90fcfb3e2429b3ed))


### Bug Fixes

* an unparsable gate verdict retries the gate instead of spawning a fix session ([#37](https://github.com/Simi24/ralph-gh/issues/37)) ([ccb7bb4](https://github.com/Simi24/ralph-gh/commit/ccb7bb45c62c49f969b21ed3a8fc7ae2bb2afc30))
* bound preflight health probe timeout, validate retry count ([#26](https://github.com/Simi24/ralph-gh/issues/26)) ([7a197f9](https://github.com/Simi24/ralph-gh/commit/7a197f9838a563eb190a05b51897aacb6a1bfaee))
* embed session id in per-iteration and per-gate state filenames ([#20](https://github.com/Simi24/ralph-gh/issues/20)) ([6e2d471](https://github.com/Simi24/ralph-gh/commit/6e2d471cc9e2ab7359243975401e327eb58f6b0e))
* escalate to SIGKILL when a timed-out claude session ignores SIGTERM ([#21](https://github.com/Simi24/ralph-gh/issues/21)) ([5cfcf57](https://github.com/Simi24/ralph-gh/commit/5cfcf574b8e0744383f099b80dba09469db10470))
* external gate no longer filters PRs by branch prefix ([#27](https://github.com/Simi24/ralph-gh/issues/27)) ([e38740f](https://github.com/Simi24/ralph-gh/commit/e38740f3bd231ef481c731a12ba8ddcdd95ff4e3))
* fail fast on missing gh push/triage permissions ([#4](https://github.com/Simi24/ralph-gh/issues/4)) ([9eaf2e4](https://github.com/Simi24/ralph-gh/commit/9eaf2e49433ed9176ff754336878442245e25b48))
* gate agent discovers any installed two-axis review skill instead of naming a private one ([0ce828a](https://github.com/Simi24/ralph-gh/commit/0ce828a765c52061b61df5131781f5009a9651d0))
* gate agent uses whatever code-review skill the user installed, no hardcoded names ([2623cb2](https://github.com/Simi24/ralph-gh/commit/2623cb284182c16f0f4e26b41059c42e44a46105))
* gate only processes PRs whose issue is ralph:needs-review ([f37a80f](https://github.com/Simi24/ralph-gh/commit/f37a80ffca8474981db068fe7e53b652fcfa0772))
* gate/fix session timeout return code overrides output-parsed verdict ([#25](https://github.com/Simi24/ralph-gh/issues/25)) ([ea4601d](https://github.com/Simi24/ralph-gh/commit/ea4601db1d915209e7b56f2e63b1358ce229f26c)), closes [#13](https://github.com/Simi24/ralph-gh/issues/13)
* harden the orchestrator after an adversarial external review ([1cabdbd](https://github.com/Simi24/ralph-gh/commit/1cabdbd69d9e8d22cd3dac62f155eb8bc1c5192f))
* make external skills optional with inline fallbacks ([1dceecb](https://github.com/Simi24/ralph-gh/commit/1dceecb4d15376155a6ebb99a34be47707393e20))
* marker prompts stop teaching backticks the parser then rejects ([#30](https://github.com/Simi24/ralph-gh/issues/30)) ([787c8f8](https://github.com/Simi24/ralph-gh/commit/787c8f8ea5164328ab285680fd8d67d469209c61))
* parse gate/fix verdicts from a channel stderr cannot pollute ([#16](https://github.com/Simi24/ralph-gh/issues/16)) ([4906fe1](https://github.com/Simi24/ralph-gh/commit/4906fe127aa91bf5c89af31fb047a3188c8bc63c))
* preflight failure and unhealthy check now abort before the loop starts ([#14](https://github.com/Simi24/ralph-gh/issues/14)) ([d8db7e6](https://github.com/Simi24/ralph-gh/commit/d8db7e612fbca910a45e03fb215d11c818d49572))
* reconcile clears residual ralph:in-progress; correct reconcile doc placement ([3098701](https://github.com/Simi24/ralph-gh/commit/3098701084e8375b8b93c1e8f0b5d401cc58aa61))
* reconcile orphaned ralph:needs-review and ralph:gate-passed issues ([#3](https://github.com/Simi24/ralph-gh/issues/3)) ([9b42501](https://github.com/Simi24/ralph-gh/commit/9b42501dea014b650a15d8b3d3fa7eccd9009ca5))
* run summary lists only issues this session touched, not every ralph:* issue ever ([#19](https://github.com/Simi24/ralph-gh/issues/19)) ([aa9d58d](https://github.com/Simi24/ralph-gh/commit/aa9d58d31a3a5cd2e21568baebfb9f6425b4f70c))
* SIGKILL in run_claude_step now targets the whole process group ([#22](https://github.com/Simi24/ralph-gh/issues/22)) ([bbf91e8](https://github.com/Simi24/ralph-gh/commit/bbf91e8853aeea0ecd352527ca9ccf57972353a5))
