"""E-sign v1 (AA-955, card CRM-25; design v3 M12, R34, R13 decline, §5.7): the agency's agreement versions rendered
with merge fields into one envelope per traveller, sealed with the platform ``seal`` key, and the signed evidence
(``crm.terms_acceptance``, ``crm.signature_event``) kept under the DM-7 claim-period exception.

- :mod:`app.esign.merge`: the merge fields an agreement text may hold, checked at draft and filled in at signing.
- :mod:`app.esign.envelope`: the canonical envelope (documents + audit page) and evidence records.
- :mod:`app.esign.seal`: the provider-neutral seal (KMS RSASSA-PSS with ``alias/crm-<env>/seal``; an HMAC stub on
  dev / test / local without a key).
- :mod:`app.esign.terms`: booking-terms acceptances (proposal, re-accept, envelope) and the re-accept rule.
- :mod:`app.esign.events`: the signing log (code sent / refused, signed).
- :mod:`app.esign.service`: the agency's view of a trip's agreements and the sealed envelope download.

The traveller side (the portal signing page) is :mod:`app.portal.public`.
"""
