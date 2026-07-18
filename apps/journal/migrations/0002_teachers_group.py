from django.db import migrations

TEACHER_GROUP_NAME = "Учителя"

GROUP_PERMISSIONS = [
    ("journal", "lesson", ("view", "add", "change")),
    ("billing", "attendance", ("view", "change")),
    ("schedule", "schedule", ("view",)),
    ("users", "student", ("view",)),
]

ACTION_LABELS = {
    "view": "Can view",
    "add": "Can add",
    "change": "Can change",
}


def create_teachers_group(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")

    group, _ = Group.objects.get_or_create(name=TEACHER_GROUP_NAME)
    for app_label, model, actions in GROUP_PERMISSIONS:
        content_type, _ = ContentType.objects.get_or_create(
            app_label=app_label, model=model
        )
        for action in actions:
            permission, _ = Permission.objects.get_or_create(
                codename=f"{action}_{model}",
                content_type=content_type,
                defaults={"name": f"{ACTION_LABELS[action]} {model}"},
            )
            group.permissions.add(permission)


def drop_teachers_group(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Group.objects.filter(name=TEACHER_GROUP_NAME).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("journal", "0001_initial"),
        ("billing", "0004_subscriptionplan_showcase_fields"),
        ("contenttypes", "0002_remove_content_type_name"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        migrations.RunPython(create_teachers_group, drop_teachers_group),
    ]
