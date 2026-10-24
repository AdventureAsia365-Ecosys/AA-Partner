"""F3: the PII filter in front of every LLM call (AA-938, design v3 card CRM-10, §4.5; lane §5.4).

- :mod:`app.privacy.text`: the diacritic- and case-folding of Vietnamese and English text with a map back to the
  original positions (names are matched folded, replaced in the original);
- :mod:`app.privacy.patterns`: pass 2, the regex detectors (signed links, e-mail, dates of birth, card numbers,
  national ids, passports, phones incl. Vietnamese formats, OTPs). The log scrubber reuses the same detectors;
- :mod:`app.privacy.scrubber`: the three passes (known values -> regex -> Bedrock Guardrails), numbered placeholders,
  the receipt (counts only) and ``restore``; any error or timeout stops the call (fail closed);
- :mod:`app.privacy.guardrails`: pass 3, ``ApplyGuardrail`` in detect mode (acc2, us-west-1, AA-930 §2);
- :mod:`app.privacy.known`: the known values of a trip (its travellers and their emergency contacts), opened through
  the vault inside the agency's context;
- :mod:`app.privacy.placeholder_store`: the placeholder map, encrypted under the tenant ``scrub`` DEK, kept 24 h and
  restored only in an agency member's session;
- :mod:`app.privacy.purge`: the hourly purge of expired maps.
"""
