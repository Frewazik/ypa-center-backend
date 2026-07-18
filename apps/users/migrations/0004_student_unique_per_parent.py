from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0003_teacherprofile_showcase_fields"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="student",
            constraint=models.UniqueConstraint(
                fields=("parent", "full_name", "dob"),
                name="uq_student_per_parent_name_dob",
            ),
        ),
    ]
