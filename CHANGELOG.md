# Changelog

## [0.4.0](https://github.com/AdventureAsia365-Ecosys/AA-Partner/compare/v0.3.0...v0.4.0) (2026-10-25)


### Features

* crm_membership_pull consumes the CIS tenant.membership.v1 feed over M2M ([#41](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/41)) ([dca754e](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/dca754e43e3d59aa3b92dcefcd3b2499c342c5f4))
* crm-012 schema for CRM-15 modules 1 + 2 (stage edges, gates, undo, client links, call slots) ([#37](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/37)) ([8169d26](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/8169d2607fe3ebe81a5248afa59a1c18e6b441bf))
* crm-013 schema for the CIS membership feed consumer (feed state, inbox, lock source) ([#39](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/39)) ([02fd757](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/02fd757956e4ce668cd4485f5a6a507b087fb63d))
* crm-014 schema for CRM-16 agency-line ledger and suppliers (lines, frozen FX money, rates) ([#42](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/42)) ([fbf9011](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/fbf9011555dab9268dbac0f7b6e27221a24fa4c4))
* crm-015 schema for CRM-17 mail (mailboxes, forward-in address, conversations, messages, drafts) ([#45](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/45)) ([5932c14](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/5932c14f9ac38dc437c7a44fe0c5386dc52f8f10))
* CRM-15 stage engine, planning form, call slots and their web UI ([#40](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/40)) ([e6bcaf4](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/e6bcaf414c3b3be3b8d780bd80fa9059b22fd611))
* CRM-16 agency-line ledger, suppliers, payment settings and daily FX fetch ([#43](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/43)) ([cb9c759](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/cb9c759aaff8a5eb2a78cfcfadc2043b60705543))
* CRM-17 agency mail (forward-in ingest, threads, send-as, Messages tab) ([#47](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/47)) ([88be0b1](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/88be0b1895f25755239bbe98765878e0e78e1a4d))
* deploy, smoke and guard the tenant portal from AA-Partner ([#46](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/46)) ([a90c1c2](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/a90c1c26b38399265547743d32a1bb3b5b43fc90))
* keep the trip's own non-name-like destinations in clear (scrubber f3-3) ([#36](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/36)) ([3f4e391](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/3f4e3914db967e3ca8c56b027cf8589a31c05bdf))

## [0.3.0](https://github.com/AdventureAsia365-Ecosys/AA-Partner/compare/v0.2.0...v0.3.0) (2026-10-24)


### Features

* CRM web skeleton, agency directory (crm-004) and the Dev seeder ([#11](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/11)) ([cf9ae8e](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/cf9ae8e4dee56c098437e22607478eab2386792b))
* crm-004 agency directory migration (label, locale, membership lookup) ([#10](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/10)) ([407cba8](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/407cba8d9e5c20a3649013ef894fa5f292eabfe9))
* crm-005 intake forms, submissions, trips, party links, tasks, notes, LLM call log ([#13](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/13)) ([c3c46b0](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/c3c46b025d0fc3d162b282a1029bf3d8144446bb))
* CRM-08 slice 1 (public intake, crm_intake_process, board, LLM gateway stub, E2E) ([#16](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/16)) ([2c21001](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/2c2100120293515550a7aef8506725849e15cc33))
* CRM-09 F1/F2 complete (reveal with reason, clients, relations, documents, F2 views) ([#19](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/19)) ([6882628](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/688262879322b70741eeb07ac65a1bdadff29823))
* CRM-11 permission matrix, partner pool sign-in, team and invitations ([#24](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/24)) ([ad3adc1](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/ad3adc1e1cf18b21059f7ae13069d05b1f793c9a))
* CRM-13a L1 platform console on /crm/ops/v1 ([#35](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/35)) ([a1d7f56](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/a1d7f56d7f030332300ec309144d06826ef25b81))
* CRM-14 data lifecycle (trash, legal holds, erasure, offboarding, purge) ([#32](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/32)) ([98b6f87](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/98b6f8747692f969b067d84e45360e86fe475669))
* F3 PII scrubber (known values, regex, Bedrock Guardrails) and the Scrub corpus workflow ([#25](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/25)) ([4a7fa4d](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/4a7fa4dfa8dfc17da44e7a479cd1a0f42c7ee284))
* F5 audit export, verification, retention and the access log page ([#23](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/23)) ([a9a93d2](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/a9a93d2ea5d77fa13bcf9bc03e643787c22a5b74))
* KeyProvider, AES-GCM field encryption, blind indexes and the crm-003 vault ([#9](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/9)) ([ee1980c](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/ee1980ceceba2f30758561855f2a8701715b966e))
* migration crm-003 (CRM vault keys and encrypted field columns) ([#7](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/7)) ([d83060e](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/d83060ef4fd9d8f321bf8438420566fb8516db49))
* migration crm-006 (F1/F2 complete: relations, health, documents, F2 views) ([#18](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/18)) ([08ef5f8](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/08ef5f8e69b7a198c9f438885d9697d9750e39b4))
* migration crm-007 (audit partitions, verified retention and the export chain) ([#20](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/20)) ([081993a](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/081993a79bfbd7335ae82fa42eea6801a614fa6d))
* migration crm-008 (roles, members, invitations and owner recovery) ([#21](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/21)) ([5ec3a0a](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/5ec3a0a9e3313356ac57813de9f9ad1fd527f18f))
* migration crm-009 (scrub placeholders and receipts) ([#22](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/22)) ([9bb1cbf](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/9bb1cbf3f2d926dcd45c15128a131fb9561a2e73))
* migration crm-010 (data lifecycle, legal holds, erase_subject) ([#31](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/31)) ([79e6abe](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/79e6abe210de83f811ad14787f9d71498bfdc419))
* migration crm-011 (insights opt-out, AA-raised data requests) ([#34](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/34)) ([2673f1b](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/2673f1b253dd1faae2d79b9a7ec1736bb7ea5038))
* typed PLACE placeholders for destinations (scrubber f3-2) ([#33](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/33)) ([f219144](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/f219144f18c6554ab375c2e36fd933f4d0083787))


### Bug Fixes

* CRM sign-out clears the session and the settings date reads the agency locale ([#14](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/14)) ([bc56927](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/bc56927f22024e61dab1e5bcabe032bde54dca23))
* CRM web listens on all interfaces (Fargate overrides HOSTNAME) ([#15](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/15)) ([ada32e5](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/ada32e5183879c6104065a75444b842895e562e2))
* CRM web opens in English by default; slice 1 E2E waits for hydration ([#17](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/17)) ([37f15b3](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/37f15b3f23cadc7ca301722c499a760471138583))
* e2e TOTP replay wait and managed login secret extraction ([#28](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/28)) ([58cc174](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/58cc17454f97fb06e4918fcdd12652fdc69c8b92))
* invite E2E withdraws invitations a failed run left pending ([#30](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/30)) ([4d9a875](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/4d9a875160a947341becc095af6db6b92a6f442c))
* Scrub corpus reads its role ARN from a secret (public run logs) ([#26](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/26)) ([ba35e05](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/ba35e050b4236daf44677610dea0fa087b255ac5))
* skip not-detected guardrail rows returned by outputScope=FULL ([#29](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/29)) ([7c1a452](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/7c1a45280b4077a021c343a292ff0404039b532c))

## [0.2.0](https://github.com/AdventureAsia365-Ecosys/AA-Partner/compare/v0.1.0...v0.2.0) (2026-10-23)


### Features

* AA-Partner skeleton (API, route classes, tenant_tx, crm-002, crm.job runner, CI) ([#1](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/1)) ([55e235a](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/55e235adc8c9f47d74a51a13b200a34c3b38c9a5))
