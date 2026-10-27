terraform {
  required_providers {
    okta = {
      source  = "okta/okta"
      version = "~> 4.0"
    }
  }
}

# Auth via environment variables (never commit tokens):
#   OKTA_ORG_NAME  = integrator-9879407
#   OKTA_BASE_URL  = okta.com
#   OKTA_API_TOKEN = <a separate token from the one n8n uses>
provider "okta" {}

# Groups built by hand in the console. Import Engineering and Support first
# (see README), then plan/apply. Contractors will be created by Terraform if
# it does not exist yet.
locals {
  groups = {
    engineering = "Engineering staff"
    support     = "Support team"
    contractors = "External contractors"
  }
}

resource "okta_group" "this" {
  for_each    = local.groups
  name        = title(each.key)
  description = each.value
}

# One user owned by Terraform, to demonstrate users-as-code.
# Users created by the n8n joiner workflow (Dana, Dana2, ...) are deliberately
# NOT declared here, so Terraform does not fight the automation.
resource "okta_user" "tf_demo" {
  first_name = "Terraform"
  last_name  = "Demo"
  login      = "tf.demo@example.com"
  email      = "tf.demo@example.com"
}

# Non-exclusive membership: adds this one user to Contractors and leaves
# every other member alone. Do NOT use okta_group_memberships (plural) on
# Support, because that resource is exclusive and would remove users the
# n8n workflow added.
resource "okta_user_group_memberships" "tf_demo" {
  user_id = okta_user.tf_demo.id
  groups  = [okta_group.this["contractors"].id]
}

output "group_ids" {
  value = { for k, g in okta_group.this : k => g.id }
}
