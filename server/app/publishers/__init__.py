"""Platform publish adapters over the vendored delivery libraries.

Only the publish worker imports this package; the FastAPI process never
loads the vendor modules or their heavy dependencies.
"""
