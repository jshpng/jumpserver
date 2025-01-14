
# Generated by Django 3.2.13 on 2022-08-24 02:57
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0039_auto_20211229_1852'),
    ]

    operations = [
        migrations.AlterField(
            model_name='user',
            name='source',
            field=models.CharField(choices=[('local', 'Local'), ('ldap', 'LDAP/AD'), ('openid', 'OpenID'), ('radius', 'Radius'), ('cas', 'CAS'), ('saml2', 'SAML2'), ('oauth2', 'OAuth2'), ('custom', 'Custom')], default='local', max_length=30, verbose_name='Source'),
        ),
    ]
