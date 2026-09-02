views {

    # View keys ARE the rendered filenames: scripts/dev/render_c4.sh strips the
    # "structurizr-" prefix, so `agnes-c4-context` lands at
    # docs/diagrams/agnes-c4-context.svg. Renaming a view renames its figure —
    # update the ARCHITECTURE.md embed in the same change.

    systemContext agnes "agnes-c4-context" "Who uses Agnes, and what Agnes talks to." {
        include *
        autoLayout
    }

    # Cloud providers are excluded here for the reason the platform model
    # excludes them: every container depends on the cloud it runs in, so the
    # same three boxes on every diagram obscure the relationships that differ.
    container agnes "agnes-c4-container" "The separately runnable pieces, and where state lives." {
        include *
        # The workstation is a separate software system, so `include *` does not
        # pull its containers in. They belong here anyway: half of what Agnes
        # does is put governed data onto that laptop.
        include analyst
        include cli
        include localAnalytics
        include claudeCode
        exclude "element.tag==Vendor"
        autoLayout tb
    }

    component api "agnes-c4-component-app" "Inside role: api — how a request becomes an authorized read." {
        include *
        exclude "element.tag==CloudProvider"
        autoLayout tb
    }

    component worker "agnes-c4-component-data" "Inside role: worker — the data path and the document path, sharing one job runtime." {
        include *
        exclude "element.tag==CloudProvider"
        exclude "element.tag==Identity"
        exclude "element.tag==Messaging"
        autoLayout tb
    }

    component gateway "agnes-c4-component-agent" "Inside role: gateway — one agent turn, and the two chokepoints in front of the model." {
        include *
        exclude "element.tag==CloudProvider"
        exclude "element.tag==DataSource"
        autoLayout tb
    }

    # -------------------------------------------------------------------------
    # Styles — aligned with the Keboola platform C4 model
    # -------------------------------------------------------------------------

    styles {
        element "Person" {
            shape Person
            background #1168bd
            color #ffffff
        }
        element "Software System" {
            background #1168bd
            color #ffffff
        }
        element "Container" {
            background #438dd5
            color #ffffff
        }
        element "Component" {
            background #85bbf0
            color #000000
        }

        element "Infrastructure" {
            background #444444
            color #ffffff
        }
        element "Datastore" {
            shape Cylinder
            background #4a6fa5
            color #ffffff
        }
        element "Ephemeral" {
            background #6a1b9a
            color #ffffff
            border dashed
        }

        element "Vendor" {
            background #1a5c33
            color #ffffff
        }
        element "Identity" {
            background #6d4c41
            color #ffffff
        }
        element "Storage" {
            background #2e7d32
            color #ffffff
        }
        element "Messaging" {
            background #b91c5c
            color #ffffff
        }
        element "AI" {
            background #6a1b9a
            color #ffffff
        }
        element "CloudProvider" {
            background #37474f
            color #ffffff
            border dashed
        }
    }

}
