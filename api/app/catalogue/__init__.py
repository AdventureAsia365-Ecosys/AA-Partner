"""AA-1069 (crm-052, S275 study item crm-23): the agency's own service catalogue.

- :mod:`app.catalogue.service`: the services (``crm.agency_service``) and their group-size net prices
  (``crm.agency_service_tier``): list, read, create, change, delete; the quantity a service takes for a party
  (:func:`app.catalogue.service.quantity`) and the option the trip builder's "Add from catalogue" writes (a snapshot:
  title, supplier, sell, net; no key back to the service).
"""
