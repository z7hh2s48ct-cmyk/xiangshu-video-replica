"""Base SQLAlchemy model definition.

This module provides the declarative base class for all ORM models
in the Superadmin Server application.
"""

from sqlalchemy import MetaData
from sqlalchemy.ext.declarative import declared_attr, declarative_base

# Naming conventions for constraints and indexes
metadata = MetaData(
    naming_convention={
        "ix": "ix_%(column_0_label)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)

Base = declarative_base(metadata=metadata)


def get_table_name(cls):
    """Generate table name from class name (e.g., TenantAdmin -> tenant_admin).
    
    Converts CamelCase to snake_case with underscores.
    
    Examples:
        >>> get_table_name(TenantAdmin)
        'tenant_admin'
        >>> get_table_name(PricingConfiguration)
        'pricing_configuration'
    """
    # Convert camel case to snake case
    name = cls.__name__
    return name[0].lower() + "".join(
        f"_ {c.lower()}" if c.isupper() else c
        for i, c in enumerate(name[1:], start=1)
    ).lstrip("_")
