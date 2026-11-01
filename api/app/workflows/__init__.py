"""Module 10 workflow automation and tasks (AA-959, card CRM-29a; design v3 M10, A-09 Tasks, A-17 Automations).

- :mod:`app.workflows.model`: the rule shape (triggers, conditions, steps) and the pure rules (validate, conditions,
  due dates, task kinds).
- :mod:`app.workflows.library`: the AA templates an owner installs.
- :mod:`app.workflows.triggers`: where runs start (stage entered, trip events, the AA-958 message seam, manual apply)
  and stop (the trip cancel, Undo).
- :mod:`app.workflows.engine`: one run's due steps (create task, send e-mail / text, set field, wait).
- :mod:`app.workflows.runner`, :mod:`app.workflows.tick`, :mod:`app.workflows.digest`: the ``crm_workflow_run``,
  ``crm_workflow_tick`` and ``crm_task_digest`` jobs.
- :mod:`app.workflows.service`, :mod:`app.workflows.tasks`: what the routes (:mod:`app.routers.workflows`) call.
"""
