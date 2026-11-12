"""Tags (AA-960, card CRM-29b; design v3 R3, R29, R36; migration crm-061).

- :mod:`app.tags.model`: the pure rules (codes, names, tones, the R29 trip system tags from AA catalogue facets, the R36
  "ready to travel again" rule and its reasons).
- :mod:`app.tags.service`: what the routes (:mod:`app.routers.tags`) call: the vocabulary, the tags of one record,
  bulk apply with its preview.
- :mod:`app.tags.system`: the system-tag refresh the hourly workflow tick runs.
- :mod:`app.tags.ready`: the R36 list (A-15 Marketing).
"""
