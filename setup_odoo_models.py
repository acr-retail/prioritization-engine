"""
One-time setup script: Create priority weight models in Odoo via API.

Creates:
  - acr.priority.attribute (name, task_field, field_type, sequence)
  - acr.priority.weight (attribute_id, value, weight, description)

Then seeds them with the default weights from the spreadsheet.

Usage:
    python setup_odoo_models.py
"""
import xmlrpc.client
import sys

ODOO_URL = "https://odoo-ps-psus-all-about-technology-sandbox-30173849.dev.odoo.com"
ODOO_DB = "odoo-ps-psus-all-about-technology-sandbox-30173849"
ODOO_USER = "darcy@allabout.technology"
ODOO_API_KEY = "8675edd840ff653b011c1e4f203dfd2c84ff928e"


def execute(models, uid, model, method, *args, **kwargs):
    return models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, model, method, *args, **kwargs)


def model_exists(models, uid, model_name):
    count = execute(models, uid, "ir.model", "search_count",
                    [[("model", "=", model_name)]])
    return count > 0


def create_model(models, uid, name, model_name, fields_spec):
    """Create a custom model with fields via ir.model."""
    print(f"\nCreating model: {model_name} ({name})")

    # Create the model
    model_id = execute(models, uid, "ir.model", "create", [{
        "name": name,
        "model": model_name,
        "state": "manual",
    }])
    print(f"  Created ir.model id={model_id}")

    # Create fields
    for field in fields_spec:
        field_vals = {
            "model_id": model_id,
            "name": "x_" + field["name"],
            "field_description": field["label"],
            "ttype": field["type"],
            "state": "manual",
        }
        if field.get("required"):
            field_vals["required"] = True
        if field.get("relation"):
            field_vals["relation"] = field["relation"]
        if field.get("on_delete"):
            field_vals["on_delete"] = field["on_delete"]
        if field.get("selection"):
            field_vals["selection_ids"] = [
                (0, 0, {"value": s[0], "name": s[1], "sequence": i})
                for i, s in enumerate(field["selection"])
            ]

        fid = execute(models, uid, "ir.model.fields", "create", [field_vals])
        print(f"  Created field x_{field['name']} (id={fid})")

    return model_id


def ensure_fields(models, uid, model_id, fields_spec):
    """Add fields that don't already exist on a model."""
    for field in fields_spec:
        fname = "x_" + field["name"]
        existing = execute(models, uid, "ir.model.fields", "search_count",
                           [[("model_id", "=", model_id), ("name", "=", fname)]])
        if existing:
            print(f"  Field {fname} already exists — skipping")
            continue

        field_vals = {
            "model_id": model_id,
            "name": fname,
            "field_description": field["label"],
            "ttype": field["type"],
            "state": "manual",
        }
        if field.get("required"):
            field_vals["required"] = True
        if field.get("relation"):
            field_vals["relation"] = field["relation"]
        if field.get("on_delete"):
            field_vals["on_delete"] = field["on_delete"]
        if field.get("selection"):
            field_vals["selection_ids"] = [
                (0, 0, {"value": s[0], "name": s[1], "sequence": i})
                for i, s in enumerate(field["selection"])
            ]

        fid = execute(models, uid, "ir.model.fields", "create", [field_vals])
        print(f"  Created field {fname} (id={fid})")


def setup_access_rights(models, uid, model_name):
    """Grant access to all internal users."""
    model_ids = execute(models, uid, "ir.model", "search",
                        [[("model", "=", model_name)]])
    if not model_ids:
        return

    # Find the base.group_user group
    group_ids = execute(models, uid, "res.groups", "search",
                        [[("full_name", "=", "Internal User")]])
    if not group_ids:
        group_ids = execute(models, uid, "res.groups", "search",
                            [[("name", "=", "Internal User")]])

    # Check if a global (no group) access rule already exists
    existing = execute(models, uid, "ir.model.access", "search",
                       [[("model_id", "=", model_ids[0]),
                         ("group_id", "=", False),
                         ("perm_create", "=", True)]])
    if existing:
        print(f"  Global access already exists for {model_name}")
        return

    # Remove any restrictive rules and create a global one
    old_rules = execute(models, uid, "ir.model.access", "search",
                        [[("model_id", "=", model_ids[0])]])
    if old_rules:
        execute(models, uid, "ir.model.access", "unlink", [old_rules])
        print(f"  Removed {len(old_rules)} old access rules for {model_name}")

    # Create access for all users (no group = global access)
    aid = execute(models, uid, "ir.model.access", "create", [{
        "name": f"access_{model_name.replace('.', '_')}_all",
        "model_id": model_ids[0],
        "perm_read": True,
        "perm_write": True,
        "perm_create": True,
        "perm_unlink": True,
    }])
    print(f"  Created global access rights (id={aid})")


def seed_weights(models, uid):
    """Seed the default weights from the spreadsheet criteria."""
    defaults = [
        ("Customer Priority", "x_studio_customer", "many2one", 1, [
            ("RonJon", 5), ("NetCost", 4), ("Rouses", 3),
            ("Shoe Carnival", 1), ("Internal", 5), ("Schnucks", 1),
        ]),
        ("Escalation Flag", "x_studio_related_field_5vi_1jnfmj9cf", "boolean", 2, [
            ("True", 1), ("False", 5),
        ]),
        ("Issue Type", "x_studio_issue_type", "selection", 3, [
            ("System stopping bug - No workaround", -15),
            ("Critical Workflow Bug- No Workaround", 1),
            ("Critical Workflow Bug- Workaround available", 2),
            ("Enhancement", 3), ("Problem", 3), ("Question", 3),
            ("Non-critical Workflow Bug", 4), ("Documentation", 5),
        ]),
        ("Customer Funded", "x_studio_related_field_gd_1jnftb4gl", "selection", 4, [
            ("Yes", -5), ("No", 5),
        ]),
        ("Level of Effort", "x_studio_level_of_effort", "selection", 5, [
            ("<10 Hrs", 1), ("10-40 Hrs", 3), ("41-100 Hrs", 4), (">100 Hrs", 5),
        ]),
        ("Roadmap Flag", "x_studio_road_map_flag", "boolean", 6, [
            ("True", 1), ("False", 5),
        ]),
        ("Paid Prioritization", "x_studio_related_field_27d_1jnftbs3p", "boolean", 7, [
            ("True", -10), ("False", 5),
        ]),
        ("Age Impact", "create_date", "computed", 8, [
            ("<30", 5), ("30-60", 3), ("60-90", 2), (">90", 1),
        ]),
    ]

    print("\nSeeding weights...")

    for name, field, ftype, seq, values in defaults:
        # Check if this attribute already exists
        existing = execute(models, uid, "x_acr_priority_attribute", "search",
                           [[("x_name", "=", name)]])
        if existing:
            print(f"  Skipping '{name}' — already exists")
            continue

        attr_id = execute(models, uid, "x_acr_priority_attribute", "create", [{
            "x_name": name,
            "x_task_field": field,
            "x_field_type": ftype,
            "x_sequence": seq,
        }])
        print(f"  Created attribute '{name}' (id={attr_id})")

        for val, weight in values:
            execute(models, uid, "x_acr_priority_weight", "create", [{
                "x_attribute_id": attr_id,
                "x_value": val,
                "x_weight": weight,
            }])
        print(f"    Added {len(values)} weight values")


def main():
    print("Connecting to Odoo...")
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_API_KEY, {})
    if not uid:
        print("ERROR: Authentication failed")
        sys.exit(1)
    print(f"Authenticated as UID {uid}")

    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")

    # --- Create acr.priority.attribute ---
    # Note: Odoo auto-creates x_name as the display/rec_name field for manual models.
    # So we use x_name for the attribute name (already exists), and add our custom fields.
    if model_exists(models, uid, "x_acr_priority_attribute"):
        print("\nModel x_acr_priority_attribute already exists — adding missing fields")
        # Ensure custom fields exist (idempotent)
        model_ids = execute(models, uid, "ir.model", "search",
                            [[("model", "=", "x_acr_priority_attribute")]])
        model_id = model_ids[0]
        ensure_fields(models, uid, model_id, [
            {"name": "task_field", "label": "Task Field", "type": "char", "required": True},
            {"name": "field_type", "label": "Field Type", "type": "selection",
             "selection": [
                 ("selection", "Selection"),
                 ("boolean", "Boolean"),
                 ("many2one", "Many2One"),
                 ("computed", "Computed"),
             ]},
            {"name": "sequence", "label": "Sequence", "type": "integer"},
        ])
    else:
        create_model(models, uid, "Priority Attribute", "x_acr_priority_attribute", [
            # x_name is auto-created by Odoo — don't create it manually
            {"name": "task_field", "label": "Task Field", "type": "char", "required": True},
            {"name": "field_type", "label": "Field Type", "type": "selection",
             "selection": [
                 ("selection", "Selection"),
                 ("boolean", "Boolean"),
                 ("many2one", "Many2One"),
                 ("computed", "Computed"),
             ]},
            {"name": "sequence", "label": "Sequence", "type": "integer"},
        ])
    setup_access_rights(models, uid, "x_acr_priority_attribute")

    # --- Create acr.priority.weight ---
    if model_exists(models, uid, "x_acr_priority_weight"):
        print("\nModel x_acr_priority_weight already exists — adding missing fields")
        model_ids = execute(models, uid, "ir.model", "search",
                            [[("model", "=", "x_acr_priority_weight")]])
        model_id = model_ids[0]
        ensure_fields(models, uid, model_id, [
            {"name": "attribute_id", "label": "Attribute", "type": "many2one",
             "relation": "x_acr_priority_attribute", "on_delete": "cascade", "required": True},
            {"name": "value", "label": "Value", "type": "char", "required": True},
            {"name": "weight", "label": "Weight", "type": "integer"},
            {"name": "description", "label": "Description", "type": "char"},
        ])
    else:
        create_model(models, uid, "Priority Weight", "x_acr_priority_weight", [
            {"name": "attribute_id", "label": "Attribute", "type": "many2one",
             "relation": "x_acr_priority_attribute", "on_delete": "cascade", "required": True},
            {"name": "value", "label": "Value", "type": "char", "required": True},
            {"name": "weight", "label": "Weight", "type": "integer"},
            {"name": "description", "label": "Description", "type": "char"},
        ])
    setup_access_rights(models, uid, "x_acr_priority_weight")

    # Bust Odoo's permission cache twice — once to flush the access rule changes,
    # then verify we can actually create weight records
    print("\nClearing Odoo permission cache...")
    execute(models, uid, "ir.config_parameter", "set_param",
            ["acr.cache_bust", str(uid)])

    # Verify access works before seeding
    print("Verifying write access...")
    can_create = execute(models, uid, "x_acr_priority_weight",
                         "check_access_rights", ["create"],
                         {"raise_exception": False})
    if not can_create:
        print("WARNING: Still no create access after cache bust. Retrying...")
        # Force another cache invalidation
        execute(models, uid, "ir.config_parameter", "set_param",
                ["acr.cache_bust2", str(uid)])
        can_create = execute(models, uid, "x_acr_priority_weight",
                             "check_access_rights", ["create"],
                             {"raise_exception": False})
        if not can_create:
            print("ERROR: Cannot get create access to x_acr_priority_weight.")
            print("Try running this script again — Odoo's cache may need a moment.")
            sys.exit(1)

    print("Access verified OK")

    # --- Seed default weights ---
    seed_weights(models, uid)

    print("\nDone! Models created and weights seeded in Odoo.")


if __name__ == "__main__":
    main()
