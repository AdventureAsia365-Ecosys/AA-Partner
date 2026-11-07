# Changelog

## [0.12.0](https://github.com/AdventureAsia365-Ecosys/AA-Partner/compare/v0.11.0...v0.12.0) (2026-11-07)


### Features

* AA prices in the CRM (the agency's AA tariff from Booking, JSON or CSV, bounded reads) ([#235](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/235)) ([a1082a9](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/a1082a9fb4184f4ffb717a71e1c493edf96944c6))
* AA prices offer only the currencies AA prices in today, and the BFF passes Retry-After on ([#236](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/236)) ([8575fad](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/8575fadacc007f6d2a82f4a2c093f590ec128b86))
* AA staff console on the CRM web (staff sign-in, S-02 ops releases, S-04 support sessions) ([#164](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/164)) ([7e45220](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/7e452205ddb2ba475e47ae6ee9ffe4b5ecf09bcb))
* AA support access L2 and break-glass L3 (owner grant, staff API, banner) ([#157](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/157)) ([9bf771e](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/9bf771e38f8076ef0bff4758e4c573ab0a9259b4))
* Account › Add-ons in the agency app (requests stored in CIS), CRM AI paused banner, invited-user Content home test ([#219](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/219)) ([0ce28ce](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/0ce28cec67dde9e6632341e2a0021d098188efa1))
* agency invoices from the payment schedule or typed lines, issue with a gapless number, void, printable document ([#131](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/131)) ([b0918d8](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/b0918d8892d62872f25fbaa47d6a11b8aa9f890d))
* AI-assisted label in Content studio and the AI mark on CRM drafts ([#207](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/207)) ([66367c1](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/66367c11487a6a3c2722db045dffcc80eb7e410f))
* Content studio and CRM web sidebar groups fold with the kit NavGroup ([#196](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/196)) ([ba3eec9](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/ba3eec9129202e5a39410fe1360296e85c041b0e))
* Content studio in English and Vietnamese (kit i18n) + studio kit-lint leftovers ([#215](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/215)) ([bda36d1](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/bda36d1fbff803f8cf4b38dd5dddf217feaffa81))
* CRM AI drafts — crm.ai_draft (crm-040), ai_assist entitlement, accept/discard UI ([#194](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/194)) ([3bd30d5](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/3bd30d51866a171fb9166f00ab27b05d321867bf))
* CRM AI gateway on the shared core with stage profiles (crm-037) and ledger v2 statuses ([#177](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/177)) ([80ade3b](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/80ade3bae2e71348e91c65e2c3299f250ce92483))
* CRM AI gateway v2 (Bedrock Converse provider, guardrail ledger rows, per-agency cap, budget alerts, Settings &gt; AI, ops LLM calls) ([#153](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/153)) ([ed154d9](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/ed154d92a1faf98fd5c4b5807106d3672d518074))
* CRM AI notice and terms of use AI section; portal uses the kit AiNotice ([#185](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/185)) ([90e8c4b](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/90e8c4b6c607edd306fb2709a60c0895f31b5ca9))
* CRM and Content studio shells on the kit ShellFrame, kit-only web and the agency visual job ([#228](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/228)) ([fa04e22](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/fa04e221fe9a8702530c7f3e51508b2839e7cecf))
* CRM app profile table (migration crm-041) ([#202](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/202)) ([35e62c1](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/35e62c1cc61fdb5261771d0b91d3de3130466e1b))
* CRM draft route on Amazon Nova Lite (acc2), migration crm-042 ([#205](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/205)) ([d2cf96d](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/d2cf96d621db81dded6b4f8c3ce4c484f03e4e0c))
* CRM gateway acts on the app profile; crm.ops.v1 1.10 app-profile routes; daily budget ops checks ([#203](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/203)) ([3dd029e](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/3dd029e3e0fd24e8e2f12fdc666e65cdfd6dd76d))
* CRM onboarding checklist, phased client import with dry run, duplicate merge, connections and controller settings ([#146](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/146)) ([132112a](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/132112aa1fbfed66d45c44905b1d993abac8e0d3))
* CRM reports (funnel, money, outstanding, team, success metrics, CSV) and AA insights read (crm-031) ([#144](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/144)) ([27f3cbb](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/27f3cbb15a21c910f7f2729495da16d962282ddf))
* CRM suggests the retail price of AA options from the agency markup or AA's RRP ([#232](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/232)) ([15c7de1](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/15c7de1cce4220e4bd7d1405aadd4a35c6ca5f44))
* crm_entitlement_pull projects the CIS tenant account; whatsapp_sms gated at send time ([#209](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/209)) ([d433581](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/d433581e2b1d6c935a089dedd4974eb1ce8bc518))
* crm-024 invoices, payments against invoices, refund requests and payment methods (migration only) ([#130](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/130)) ([db9f491](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/db9f491f831366e8dd7f5de6b544614aff839398))
* crm-025 e-sign v1 schema (signing envelope, guardian signer, terms acceptances, signing log, claim-period retention) ([#135](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/135)) ([09b1352](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/09b135276cf96a3e1982826b83c5b2b6e25e2744))
* crm-026 portability exports, data-request log and agency-deletion cool-off (migration only) ([#137](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/137)) ([278c44c](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/278c44ce0671523f65be8d6801eb5ebbbbad34d5))
* crm-027 duty of care and calendar schema (AA incident flags per departure, revocable ICS calendar feeds per member) ([#139](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/139)) ([82ce4aa](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/82ce4aa62e2b05056bdc0d27f325a6440e33c6f8))
* crm-032 crm.llm_call ledger v2 columns, guard role and AI budget alert state ([#152](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/152)) ([361071d](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/361071d0b690b17edf01201b53e97c3bb99e16c4))
* crm-033 AA support grants and sessions (contract) ([#156](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/156)) ([a7bb39f](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/a7bb39fae644c0266cfd22470c973390e6a6378c))
* crm-035 commission and trip cancellation (contract) ([#165](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/165)) ([94b20b7](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/94b20b766093d9070582c86a41874ea92efa8479))
* crm-037 AI stage profiles (route, attempts, deadline, kill switch) and ledger v2 statuses ([#176](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/176)) ([b8223f3](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/b8223f3713b4fae085fac2903d41ccb44f6149e5))
* crm-038 workflow rules, workflow runs and the task columns of Module 10 ([#182](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/182)) ([0b3f23b](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/0b3f23bfea11f25a1a6f33cf1ede228261bdf436))
* crm-039 lets the partner-account clean-up job read Content studio invitations ([#188](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/188)) ([4ee3cf7](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/4ee3cf765a01fadf538407b2c3cb30bfa8cfad5c))
* crm-043 storage for crm_entitlement_pull (feed state, projected_at, cis_tenant from the account feed) ([#208](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/208)) ([4a4d76d](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/4a4d76d4c95b20df0df2f2fe91f9e467a1b413da))
* crm-045 keeps the studio invitation address under the CRM data key (contract) ([#226](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/226)) ([e666ec4](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/e666ec4e3b0806e01601797057600cad2dacb561))
* crm-046 agency default retail markup per AA product type (tenant_settings.retail_markup_pct) ([#231](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/231)) ([8834221](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/8834221c3269fec17a51d3d74bfd3df5dc322175))
* CRM-26 Module 8 ops release (release, refresh, T-21 alert, withdraw, purge, S-02 staff API) ([#160](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/160)) ([cceed8e](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/cceed8e53938200bbf399733d5b259b1f32cba21))
* CRM-27 commission tracking and one-action trip cancel (Booking fee, refunds, pending until AA confirms) ([#166](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/166)) ([de8d640](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/de8d640f904558c421a9af4de2443fe5790d958e))
* CRM-28 SMS and WhatsApp on the agency's own Twilio (crm-036, webhooks, composer, settings) ([#175](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/175)) ([0818573](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/0818573e42b08a6771f8070bb9cd6d4b0acd2a07))
* CRM-29a Module 10 workflow engine, Tasks and Automations pages ([#183](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/183)) ([d2e20a4](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/d2e20a4da3fef4758f5e7899c8cdd8230aa2e8ed))
* crm.ops.v1 1.12 desk-staff scope from CIS, send again to the kept address, schema_gate byte copy ([#227](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/227)) ([6ee51eb](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/6ee51eb72d0be8727060183af0cfb091ef518e03))
* crm.tenant.is_pilot, the CIS pilot marker written by the account feed only (crm-044) ([#223](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/223)) ([2ba1f48](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/2ba1f48163e5cdec0cfcbcec35da1b53e583ce1a))
* data portability exports, a data-request log, verified client erasure and a cool-off before agency deletion ([#138](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/138)) ([7e9d8db](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/7e9d8db053d5d51707827ea7ef8e465454ca6832))
* delete never-signed-in partner-pool accounts nobody needs (crm_partner_accounts) ([#189](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/189)) ([f7e8d38](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/f7e8d3816c1a5f8f095ed079b32a2815b3a14bd6))
* duty of care and calendar (travelling now by region, AA incident banners, logged bulk reveal, agency calendar, revocable ICS feed) ([#140](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/140)) ([3718242](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/3718242a264c9ffe9bbb7f0db468c58deb866d73))
* e-sign v1 for contracts and waivers (one sealed envelope per traveller, guardian signing, terms re-accept, signed documents panel) ([#136](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/136)) ([021d68e](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/021d68e69175077db995f1c0b649a3be18b425b5))
* in-app ops checks replace the CRM CloudWatch alarms (S263 alert diet) ([#181](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/181)) ([50042c1](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/50042c12d6691d7f6d4196d6ed0d3ff1d5db7148))
* installable traveller page with an itinerary-only offline copy (CRM-24b) ([#161](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/161)) ([f6601a0](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/f6601a008f888e1c335edcfd3d90accebf8ad4a1))
* invoice claim, one hold extension, re-hold after a late deposit, overdue reminders (CRM-23 slice 4) ([#143](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/143)) ([26acce0](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/26acce08f628d56ed6a235bbb0925c35ccfc36b6))
* merge the Content studio into the agency web app (R3 slice 1) ([#210](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/210)) ([93977ff](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/93977fffbff5d0a96beb89bcdb89faa21031419c))
* payments against invoices, refund requests with the approval matrix, the agency's own payment methods ([#132](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/132)) ([8525a21](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/8525a211adef04cf627e442ca91ad5626a2f98ba))
* pilot agency protections: clean-up skips it, nightly backup export, restore drill ([#224](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/224)) ([a7039b0](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/a7039b09b5d6a937a0c102575a0b43c8867d18f7))
* refuse migration references left behind by a renumber, and a push mid-rebase ([#241](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/241)) ([38f99f5](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/38f99f53841a49759a08f74d4d1ec08119d9ee7a))
* Settings &gt; Add-ons in the CRM, the owner's add-on request relayed to CIS tenant.addons.v1 ([#221](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/221)) ([82bdba9](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/82bdba99fc6bf075d06882165ec16fc750f44fe9))
* studio drops the API page, key card and API-key sign-in paths ([#213](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/213)) ([6c36fc8](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/6c36fc81835534b14172b8ddf4f561a21c819793))
* the Content studio Marketplace shows the price.from.v1 from price on the card and in the drawer ([#214](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/214)) ([0b20abf](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/0b20abfdb401ef18af7e6e6904047e169e432fa2))
* the Dev-only customer_success E2E identity writes nothing in the CRM (A10d) ([#234](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/234)) ([7131198](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/7131198736d78df669c4f95aee41a7b3793fd3ea))
* the web deploy verifies and smoke-tests the Content studio on the agency host (AA-1036 cut-over) ([#211](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/211)) ([62b2c39](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/62b2c391e2f17866cb05cebef3e148322634e09e))
* traveller portal per person (picker, flights, documents, passport scan, companion invite) ([#145](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/145)) ([df5ac86](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/df5ac86f257048d118063e467741abd0b20a57e4))


### Bug Fixes

* a cancelled trip bills no more; cancel dialog and A-12 commissions polish (S262 live findings) ([#168](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/168)) ([9ca6997](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/9ca69971aefc3dd670d13963bccd207968e569be))
* a cancelled trip is closed for planning on every card; plurals, lead source labels, /payments tabs ([#169](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/169)) ([eeccc98](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/eeccc98c0cdb828ee7e4664b4af3ad6a20dfb834))
* agency export opens every sealed column under its writer's DEK; failed pilot backup alerts ([#230](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/230)) ([7d4798c](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/7d4798ce4ee4c5068cc756f99a76157ca3fb3839))
* AI settings say the privacy check is billed and show the plan state ([#201](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/201)) ([0997e46](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/0997e46be43fe3a938428d9fe7e0d6c05e2962bd))
* builder on retired booking terms, phone cards for trip bookings, read-only money while the cancel run applies (S262 round 3) ([#170](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/170)) ([2787b45](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/2787b4515e7a61aa74a74fdc3a717343227cacfd))
* calendar times in the agency timezone and a singular signature count ([#142](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/142)) ([3a11651](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/3a116512dd27fdb25fee0f9c53482f8a2dc2fc7c))
* client erasure refusal names what unblocks it ([#141](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/141)) ([80870f8](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/80870f8053626930459dcf996733c4e111e04a76))
* client pay box waits for re-accepted terms, builder versions pin the published terms, claim deadline in the agency timezone ([#148](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/148)) ([ba225e4](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/ba225e4bf9093865c5838803baf3f03cdb206ace))
* Content studio CRM sign-in via GET /bff/identity (always 200, no console errors) ([#193](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/193)) ([613f4df](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/613f4df7fd6ebb13221f73728d47864dd7b01183))
* Content studio shows the CRM sign-in, support address labelled; studio link only with a studio ([#191](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/191)) ([3e7b2e9](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/3e7b2e9027317f280331e8e6af107eb2b2f88abe))
* create agency on a deleted agency's id answers 409 tenant_deleted ([#197](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/197)) ([22d833c](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/22d833cb4bdf03be75dc229ad8494c12335ba54b))
* CRM Dev E2E waits for the held AA line and proves settings saves after a reload ([#163](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/163)) ([e4cdcfc](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/e4cdcfce773c90271046dd412d5f1462c38333b4))
* CRM hardening (body ceiling, agency fair share, fair crm.job claim, log redaction) + SLO doc and bounded load test ([#218](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/218)) ([e333af5](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/e333af534ac268f1f41d74c58e945bf8b4eb2165))
* CRM web downloads renew an expired access token before failing ([#172](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/172)) ([d395b7a](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/d395b7ac47faadadb0abf01daadbf0b30ce6c1f9))
* crm-036 has no migrate directive (a contract file is a file without the expand directive) ([#180](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/180)) ([e20455b](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/e20455ba35e182e7efbedccd44a4d42b24053ade))
* crm-036 header is contract (applied by hand with DB Migrate) ([#179](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/179)) ([6714a23](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/6714a238bbe5188efffbc67af7743422beb82ed2))
* join page keeps the one-time password; AA withdraws a first-owner invitation and deletes a test agency ([#186](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/186)) ([e5d5d51](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/e5d5d517def6d288177f8e2326ad6ea88b4dc422))
* load-test guard stops on a momentary CIS job condition only when two sampler lines show it ([#222](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/222)) ([e20a684](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/e20a684367dbdff311d79c069a3b0926e239a919))
* migration reference check reads non-UTF-8 diff lines; pin that it runs on every PR ([#243](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/243)) ([e0f46eb](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/e0f46ebca34308cab997821ee781e24f5689c1be))
* never hand mail for a .invalid address to a provider ([#150](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/150)) ([bc5f928](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/bc5f9280dd0b680fcd85f6750a0c9bba90cc2562))
* separate per-address write limit for the signed-in traveller portal ([#149](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/149)) ([d29e29e](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/d29e29e628ec65a74a587361b60ea7feb9ef3f0f))
* serve the CRM terms at /legal/terms, outside the studio's agency-host paths ([#192](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/192)) ([9689bc8](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/9689bc8a09abe0615038a316257e7701d90369ac))
* staff support console says why a session ended; settings card with labels and formatted values ([#171](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/171)) ([f2f72e9](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/f2f72e921393065bd0dd8624b54e41c2a2bc67a8))
* studio BFF passes the AI mark headers and a download's file name ([#242](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/242)) ([c99afc8](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/c99afc809b00b4d081e9721630ce39a17b0bfebf))
* studio smoke expects the partner sign-in activity row ([#217](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/217)) ([3485731](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/3485731499f9fe2e9e683bc27aec31e8a69de633))
* the temporary password re-issue names the account by its e-mail address (e-mail-username pool) ([#187](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/187)) ([3fd7d08](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/3fd7d085ae656df304b6a460cd48e04d7e835e1e))
* trip page words every task kind and stage reason, names incident departures, keeps the dark theme ([#167](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/167)) ([9e09545](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/9e09545e0c8fc045f4b681934cc4f0dbc74660da))
* trip panel activity lines name the trip instead of a dangling object ([#162](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/162)) ([83c1aac](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/83c1aac69e88ba03d131c997d5ff7dc72aa886c3))
* upgrade the API image's Debian packages (apt fix the base image lacks) ([#225](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/225)) ([6d560d5](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/6d560d59aa2000c99576d6061c76e9f5d55a2f79))

## [0.11.0](https://github.com/AdventureAsia365-Ecosys/AA-Partner/compare/v0.10.0...v0.11.0) (2026-10-30)


### Features

* AA lines from the accepted hold set and Booking confirm after the deposit ([#128](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/128)) ([c572c6e](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/c572c6e140253d59e22b2d826027c507506381d6))


### Bug Fixes

* schema gate accepts a newer green standalone isolation probe after a probe-only red run ([#126](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/126)) ([09efb37](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/09efb370beb39d99522e883387998290b128ea65))

## [0.10.0](https://github.com/AdventureAsia365-Ecosys/AA-Partner/compare/v0.9.1...v0.10.0) (2026-10-29)


### Features

* Content studio Facts page for an agency's own sourced facts ([#120](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/120)) ([767b686](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/767b686b061b989a07bed28de5fe5e92679b49bd))
* Deploy Dev schema-gate and CI Migration preflight ([#121](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/121)) ([03918c0](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/03918c0bc4754c73a8f771641b804424491f97c1))

## [0.9.1](https://github.com/AdventureAsia365-Ecosys/AA-Partner/compare/v0.9.0...v0.9.1) (2026-10-29)


### Bug Fixes

* marketplace price chip icon no longer shrinks to a stray dot; tiers on one line ([#117](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/117)) ([fac5ea8](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/fac5ea8e6db7e484186eed8dee20540931428c04))

## [0.9.0](https://github.com/AdventureAsia365-Ecosys/AA-Partner/compare/v0.8.0...v0.9.0) (2026-10-29)


### Features

* Marketplace activity, intensity and recently-added filters, and responsive tour cover photos ([#111](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/111)) ([09c562b](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/09c562b95e530a14684b82895d06110f9b3e8890))
* registered_keys on /crm/health/ready ([#112](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/112)) ([c5c37cf](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/c5c37cfc532db0b6fa0bdca466f4bdbebcd17feb))

## [0.8.0](https://github.com/AdventureAsia365-Ecosys/AA-Partner/compare/v0.7.0...v0.8.0) (2026-10-28)


### Features

* crm_dispatch commits a send marker before the provider call, so it no longer blocks deploys ([#107](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/107)) ([cc633a4](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/cc633a40a001f2a906f47375e716030c02c36936))
* crm-023 dispatch attempt send marker table ([#105](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/105)) ([419dd8e](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/419dd8e424055247cf15edb2c6821c7452355408))
* kit lint preset at error in the portal and the CRM web with shrinking baselines ([#108](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/108)) ([6221660](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/6221660d024e6912406626cd0ba753e614dfd098))
* one Marketplace page in the portal (catalog grid, filters, detail drawer) ([#109](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/109)) ([a33ccf4](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/a33ccf4a8fcb69dac77ce64c5dde6dac53e02876))


### Bug Fixes

* remove internal issue IDs from user-visible text ([#110](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/110)) ([2556d31](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/2556d31a4d3ad216a8527e675dc647ebecba2704))

## [0.7.0](https://github.com/AdventureAsia365-Ecosys/AA-Partner/compare/v0.6.0...v0.7.0) (2026-10-27)


### Features

* deploy gate blocks only while crm_dispatch runs (AA-993 G5) ([#99](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/99)) ([826df53](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/826df53612184fe36368620d7b8a1fa95982c4c5))
* public trip page on the client host (/trip/[tourId]) ([#100](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/100)) ([2fb453f](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/2fb453f765fab09e30700daa43a02d5a88f42d6e))
* remove the Dev EXTERNAL cis-portal bridge on /crm/v1 ([#101](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/101)) ([c35ad2d](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/c35ad2d5901c4014667060262f4a6918f150e002))
* vendor the UI kit core into the portal and the CRM web ([#102](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/102)) ([8af302e](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/8af302ee1d2ae8b3c9b511359459c52aed5f4bc8))


### Bug Fixes

* trip page imports brand constants from the kit core ([#104](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/104)) ([3d33850](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/3d338504ad0a1feb753d92d03c512f6036b881b3))

## [0.6.0](https://github.com/AdventureAsia365-Ecosys/AA-Partner/compare/v0.5.0...v0.6.0) (2026-10-27)


### Features

* client proposal, accept and hold (CRM-21b) ([#78](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/78)) ([8c255be](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/8c255bee114f665eab117ea13d835814b621fd28))
* Content studio on Fargate with the partner-pool sign-in ([#73](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/73)) ([6e235cd](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/6e235cd0f25f2a90660d9cf9a054be61f022190b))
* CRM web and Content studio post-deploy verify, Fargate UI smoke ([#97](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/97)) ([76b53da](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/76b53da3869c276b0e0fc603d1b3cb86d287429f))
* CRM web answers both agency hosts with a per-host public origin ([#72](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/72)) ([3b2b7f2](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/3b2b7f2ab01bc87edafa96188de0ef7cf2d4411a))
* crm-019 proposal builder model (migration only) ([#65](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/65)) ([832e784](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/832e784b44425a5a678dd797bd8bac25e4fe2e42))
* crm-020 studio invitation table for Content studio re-enrolment ([#70](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/70)) ([9e1a5bb](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/9e1a5bba1eae15a211d532c86d144f3c224d9810))
* crm-021 client proposal model (migration only) ([#77](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/77)) ([13e42b9](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/13e42b92a0dab0a9759097a0d194afd61109cda8))
* crm-022 AA content cache, day 0 prep packs and message templates ([#95](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/95)) ([4e7e76a](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/4e7e76ae4f340b12216fc9030e9b4fa64bf9a685))
* crm.ops.v1 1.4 data coverage counts for the CIS admin page ([#75](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/75)) ([7e3a38d](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/7e3a38d5d8dbe44f9903b3c29ebe38cb0c141d52))
* day-list proposal builder (CRM-21a) ([#66](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/66)) ([66ffef7](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/66ffef7dfb41a290135dbdcb3d4c34b91b01b7ed))
* deliver CIS Content studio invitations and activate them at CIS ([#74](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/74)) ([61e0d26](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/61e0d26b2b536308af854df8b7b07c4c27b44127))
* mail history import in Settings &gt; Mailbox ([#88](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/88)) ([86082f4](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/86082f4cb2387da408cdfeed170cbcf920b0f467))
* notice when Adventure Asia updated a tour after the tenant's version ([#89](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/89)) ([65b41dc](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/65b41dc0f77775ba00898b5b43214db893866e40))
* portal shows angle requests whose tour part was rewritten and turns Write off ([#63](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/63)) ([7672645](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/767264533b1d844a57c8877c70c06c8f97690fe8))
* text proposals by e-mail and WhatsApp, Before you go packs and the template library ([#96](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/96)) ([ae8104f](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/ae8104f523e41900493721214be61b8147077d9d))
* the A10b studio-invitation check requires CIS is_synthetic (tenant.membership.v1 1.2) ([#84](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/84)) ([5f66e03](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/5f66e03c61960394d615a3d94662a7335fe1272f))


### Bug Fixes

* a refused tenant token on the agency host goes to the partner sign-in ([#76](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/76)) ([42c17a0](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/42c17a0bc8d23695df22d72061f2f97f58e2618e))
* end References with the CRM's own Message-ID so replies thread when Gmail replaces it ([#86](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/86)) ([d39f6da](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/d39f6dad6037484b977d6e4d7c881a718be4d054))
* mail import type select stays inside its column on phones ([#90](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/90)) ([7bd2bb0](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/7bd2bb089d959059915b82ede0313e992f472641))
* put the Refresh AA prices test id on a wrapper (the kit Button drops data-testid) ([#68](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/68)) ([4b289dc](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/4b289dcd5da94a2ddb0f0b5b9137805dd671149e))
* refuse the Dev-only E2E CIS admin on crm.ops.v1 writes outside synthetic studio invitations ([#83](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/83)) ([0a9beb3](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/0a9beb303d742d47510b381059cc3fa4ae7861c7))
* studio sign-out reaches the managed-login logout page ([#94](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/94)) ([bed7404](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/bed74040c545807d719ae6ee3f06379cb29f33aa))

## [0.5.0](https://github.com/AdventureAsia365-Ecosys/AA-Partner/compare/v0.4.0...v0.5.0) (2026-10-25)


### Features

* AA-978 follow-ups (crm-018 inbox purge, ops summary lock source and feed lag, portal Members) ([#54](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/54)) ([e83aef5](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/e83aef55ea3926f3a5a02cd66eb55fcaaa338eec))
* crm-016 thin lifecycle tables, computable gates and Stage 3 lock ([#49](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/49)) ([6ccb9dc](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/6ccb9dc5a7ea1c04a98047893abae7c50cbcee31))
* crm-017 AA lines, outage queue, Booking feed inbox and catalog cache ([#56](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/56)) ([dd8557b](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/dd8557b9c3f6d07a0c91b1203178411f55baddca))
* CRM-18 thin full lifecycle (proposal accept, mark paid, traveller portal, e-sign v0) ([#50](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/50)) ([b5c99ee](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/b5c99eec203ab7176a762599d8941ac0dbbadff1))
* CRM-19 B2B client and AA lines (quote/hold/feed, outage queue, catalog cache) ([#55](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/55)) ([ccd8baa](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/ccd8baa4eca43ac4b2d083d5898d7bfae0adddf9))
* My Content table with place, angle, words, published columns, views and row actions ([#53](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/53)) ([3973f13](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/3973f13c3337fe935fff754db4211871edf44a44))
* portal Account pages with real data (billing meter, activity trail, integrations and API key) ([#51](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/51)) ([0f8cb1b](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/0f8cb1b0b7b6c45e2d0ca1e9a23c0d7b8a4e8966))


### Bug Fixes

* My Content table fits 1440 without the angle brief; piece title, activity format labels, aria-sort, link contrast ([#59](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/59)) ([9b5d488](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/9b5d4889d907526edf81b10ceb8c4c0b8a893134))
* write gitleaks sample Google API keys without a literal key ([#57](https://github.com/AdventureAsia365-Ecosys/AA-Partner/issues/57)) ([214af96](https://github.com/AdventureAsia365-Ecosys/AA-Partner/commit/214af96162da9bad178a2e21086cfbcfdb40b62e))

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
