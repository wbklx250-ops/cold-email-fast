"""Add all missing columns to sync models with database

Revision ID: 011
Revises: 010
Create Date: 2026-03-17

This migration adds ALL columns that exist in the SQLAlchemy models but are
missing from the database. This is the root cause of pipeline Steps 5-9
silently skipping all work -- queries reference non-existent columns, throw
exceptions caught by broad try/except blocks, and steps go green with zero
actual work done.

Uses IF NOT EXISTS / ADD COLUMN IF NOT EXISTS throughout so it is safe to
run even if some columns were added manually.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '011'
down_revision: Union[str, None] = '010'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ================================================================
    # ENUM UPDATES
    # ================================================================

    # Add missing batch_status enum values
    op.execute("ALTER TYPE batch_status ADD VALUE IF NOT EXISTS 'in_progress';")

    # Add missing tenant_status enum values
    op.execute("ALTER TYPE tenant_status ADD VALUE IF NOT EXISTS 'first_login_pending';")
    op.execute("ALTER TYPE tenant_status ADD VALUE IF NOT EXISTS 'first_login_complete';")
    op.execute("ALTER TYPE tenant_status ADD VALUE IF NOT EXISTS 'domain_added';")
    op.execute("ALTER TYPE tenant_status ADD VALUE IF NOT EXISTS 'dns_configured';")
    op.execute("ALTER TYPE tenant_status ADD VALUE IF NOT EXISTS 'pending_dkim';")
    op.execute("ALTER TYPE tenant_status ADD VALUE IF NOT EXISTS 'mailboxes_created';")
    op.execute("ALTER TYPE tenant_status ADD VALUE IF NOT EXISTS 'ready';")

    # Add missing domain_status enum values
    op.execute("ALTER TYPE domain_status ADD VALUE IF NOT EXISTS 'zone_created';")
    op.execute("ALTER TYPE domain_status ADD VALUE IF NOT EXISTS 'ns_propagated';")
    op.execute("ALTER TYPE domain_status ADD VALUE IF NOT EXISTS 'tenant_linked';")
    op.execute("ALTER TYPE domain_status ADD VALUE IF NOT EXISTS 'm365_verified';")
    op.execute("ALTER TYPE domain_status ADD VALUE IF NOT EXISTS 'error';")

    # Add missing mailbox_status enum values
    op.execute("ALTER TYPE mailbox_status ADD VALUE IF NOT EXISTS 'pending';")
    op.execute("ALTER TYPE mailbox_status ADD VALUE IF NOT EXISTS 'enabled';")
    op.execute("ALTER TYPE mailbox_status ADD VALUE IF NOT EXISTS 'password_set';")
    op.execute("ALTER TYPE mailbox_status ADD VALUE IF NOT EXISTS 'upn_fixed';")
    op.execute("ALTER TYPE mailbox_status ADD VALUE IF NOT EXISTS 'delegated';")
    op.execute("ALTER TYPE mailbox_status ADD VALUE IF NOT EXISTS 'error';")

    # ================================================================
    # SETUP_BATCHES TABLE — missing columns
    # ================================================================

    op.execute("ALTER TABLE setup_batches ADD COLUMN IF NOT EXISTS domains_per_tenant INTEGER NOT NULL DEFAULT 1;")
    op.execute("ALTER TABLE setup_batches ADD COLUMN IF NOT EXISTS sequencer_app_key VARCHAR(50) NOT NULL DEFAULT 'instantly';")
    op.execute("ALTER TABLE setup_batches ADD COLUMN IF NOT EXISTS completed_steps JSONB DEFAULT '[]';")
    op.execute("ALTER TABLE setup_batches ADD COLUMN IF NOT EXISTS uploaded_to_sequencer BOOLEAN NOT NULL DEFAULT FALSE;")
    op.execute("ALTER TABLE setup_batches ADD COLUMN IF NOT EXISTS uploaded_at TIMESTAMP WITH TIME ZONE;")
    op.execute("ALTER TABLE setup_batches ADD COLUMN IF NOT EXISTS auto_progress_enabled BOOLEAN NOT NULL DEFAULT FALSE;")
    op.execute("ALTER TABLE setup_batches ADD COLUMN IF NOT EXISTS auto_run_state JSONB;")
    op.execute("ALTER TABLE setup_batches ADD COLUMN IF NOT EXISTS step6_emails_generated BOOLEAN NOT NULL DEFAULT FALSE;")
    op.execute("ALTER TABLE setup_batches ADD COLUMN IF NOT EXISTS step6_emails_generated_at TIMESTAMP WITH TIME ZONE;")
    op.execute("ALTER TABLE setup_batches ADD COLUMN IF NOT EXISTS custom_mailbox_map JSONB;")
    op.execute("ALTER TABLE setup_batches ADD COLUMN IF NOT EXISTS new_admin_password VARCHAR;")
    op.execute("ALTER TABLE setup_batches ADD COLUMN IF NOT EXISTS sequencer_platform VARCHAR;")
    op.execute("ALTER TABLE setup_batches ADD COLUMN IF NOT EXISTS sequencer_login_email VARCHAR;")
    op.execute("ALTER TABLE setup_batches ADD COLUMN IF NOT EXISTS sequencer_login_password VARCHAR;")
    op.execute("ALTER TABLE setup_batches ADD COLUMN IF NOT EXISTS profile_photo_path VARCHAR;")
    op.execute("ALTER TABLE setup_batches ADD COLUMN IF NOT EXISTS pipeline_status VARCHAR DEFAULT 'not_started';")
    op.execute("ALTER TABLE setup_batches ADD COLUMN IF NOT EXISTS pipeline_step INTEGER DEFAULT 0;")
    op.execute("ALTER TABLE setup_batches ADD COLUMN IF NOT EXISTS pipeline_step_name VARCHAR;")
    op.execute("ALTER TABLE setup_batches ADD COLUMN IF NOT EXISTS pipeline_started_at TIMESTAMP WITH TIME ZONE;")
    op.execute("ALTER TABLE setup_batches ADD COLUMN IF NOT EXISTS pipeline_completed_at TIMESTAMP WITH TIME ZONE;")
    op.execute("ALTER TABLE setup_batches ADD COLUMN IF NOT EXISTS pipeline_paused_at TIMESTAMP WITH TIME ZONE;")
    op.execute("ALTER TABLE setup_batches ADD COLUMN IF NOT EXISTS ns_confirmed_at TIMESTAMP WITH TIME ZONE;")
    op.execute("ALTER TABLE setup_batches ADD COLUMN IF NOT EXISTS total_domains INTEGER NOT NULL DEFAULT 0;")
    op.execute("ALTER TABLE setup_batches ADD COLUMN IF NOT EXISTS total_tenants INTEGER NOT NULL DEFAULT 0;")
    op.execute("ALTER TABLE setup_batches ADD COLUMN IF NOT EXISTS zones_completed INTEGER NOT NULL DEFAULT 0;")
    op.execute("ALTER TABLE setup_batches ADD COLUMN IF NOT EXISTS ns_propagated_count INTEGER NOT NULL DEFAULT 0;")
    op.execute("ALTER TABLE setup_batches ADD COLUMN IF NOT EXISTS dns_completed INTEGER NOT NULL DEFAULT 0;")
    op.execute("ALTER TABLE setup_batches ADD COLUMN IF NOT EXISTS first_login_completed_count INTEGER NOT NULL DEFAULT 0;")
    op.execute("ALTER TABLE setup_batches ADD COLUMN IF NOT EXISTS m365_completed INTEGER NOT NULL DEFAULT 0;")
    op.execute("ALTER TABLE setup_batches ADD COLUMN IF NOT EXISTS mailboxes_completed_count INTEGER NOT NULL DEFAULT 0;")
    op.execute("ALTER TABLE setup_batches ADD COLUMN IF NOT EXISTS smtp_completed INTEGER NOT NULL DEFAULT 0;")
    op.execute("ALTER TABLE setup_batches ADD COLUMN IF NOT EXISTS sequencer_uploaded_count INTEGER NOT NULL DEFAULT 0;")
    op.execute("ALTER TABLE setup_batches ADD COLUMN IF NOT EXISTS errors_count INTEGER NOT NULL DEFAULT 0;")

    # ================================================================
    # TENANTS TABLE — missing columns
    # ================================================================

    # Admin credentials
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS initial_password VARCHAR(255);")
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS totp_secret VARCHAR(255);")

    # Contact info
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS address VARCHAR(500);")
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS contact_name VARCHAR(255);")
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS contact_email VARCHAR(255);")
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS contact_phone VARCHAR(50);")

    # First login tracking
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS first_login_completed BOOLEAN NOT NULL DEFAULT FALSE;")
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS first_login_at TIMESTAMP WITH TIME ZONE;")
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS password_changed BOOLEAN NOT NULL DEFAULT FALSE;")
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS security_defaults_disabled BOOLEAN NOT NULL DEFAULT FALSE;")
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS security_defaults_error TEXT;")
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS security_defaults_disabled_at TIMESTAMP WITH TIME ZONE;")

    # Licensed user
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS licensed_user_upn VARCHAR(255);")
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS licensed_user_password VARCHAR(255);")
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS licensed_user_created BOOLEAN NOT NULL DEFAULT FALSE;")
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS license_assigned BOOLEAN NOT NULL DEFAULT FALSE;")

    # Custom domain
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS custom_domain VARCHAR(255);")

    # M365 domain setup
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS domain_added_to_m365 BOOLEAN NOT NULL DEFAULT FALSE;")
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS m365_verification_txt VARCHAR(255);")
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS domain_verified_in_m365 BOOLEAN NOT NULL DEFAULT FALSE;")
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS domain_verified_at TIMESTAMP WITH TIME ZONE;")

    # DNS tracking
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS mx_record_added BOOLEAN NOT NULL DEFAULT FALSE;")
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS spf_record_added BOOLEAN NOT NULL DEFAULT FALSE;")
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS autodiscover_added BOOLEAN NOT NULL DEFAULT FALSE;")

    # DKIM tracking
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS dkim_selector1 VARCHAR(500);")
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS dkim_selector2 VARCHAR(500);")
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS dkim_cnames_added BOOLEAN NOT NULL DEFAULT FALSE;")
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS dkim_enabled BOOLEAN NOT NULL DEFAULT FALSE;")
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS dkim_enabled_at TIMESTAMP WITH TIME ZONE;")
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS dkim_retry_count INTEGER NOT NULL DEFAULT 0;")
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS dkim_last_retry_at TIMESTAMP WITH TIME ZONE;")

    # Mailbox tracking
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS mailboxes_generated BOOLEAN NOT NULL DEFAULT FALSE;")
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS mailboxes_created BOOLEAN NOT NULL DEFAULT FALSE;")

    # Fix mailboxes_created if it was created as integer by an earlier migration (001)
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'tenants'
                AND column_name = 'mailboxes_created'
                AND data_type = 'integer'
            ) THEN
                ALTER TABLE tenants ALTER COLUMN mailboxes_created
                TYPE BOOLEAN USING CASE WHEN mailboxes_created = 0 THEN FALSE ELSE TRUE END;
                ALTER TABLE tenants ALTER COLUMN mailboxes_created SET DEFAULT FALSE;
            END IF;
        END $$;
    """)

    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS mailboxes_created_at TIMESTAMP WITH TIME ZONE;")
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS delegation_completed BOOLEAN NOT NULL DEFAULT FALSE;")
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS mailbox_count INTEGER NOT NULL DEFAULT 0;")

    # Step 5 tracking
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS step5_complete BOOLEAN NOT NULL DEFAULT FALSE;")
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS step5_completed_at TIMESTAMP WITH TIME ZONE;")

    # Step 6 tracking
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS step6_started BOOLEAN NOT NULL DEFAULT FALSE;")
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS step6_started_at TIMESTAMP WITH TIME ZONE;")
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS step6_mailboxes_created INTEGER NOT NULL DEFAULT 0;")
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS step6_display_names_fixed INTEGER NOT NULL DEFAULT 0;")
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS step6_accounts_enabled INTEGER NOT NULL DEFAULT 0;")
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS step6_passwords_set INTEGER NOT NULL DEFAULT 0;")
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS step6_upns_fixed INTEGER NOT NULL DEFAULT 0;")
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS step6_delegations_done INTEGER NOT NULL DEFAULT 0;")
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS step6_complete BOOLEAN NOT NULL DEFAULT FALSE;")
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS step6_completed_at TIMESTAMP WITH TIME ZONE;")
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS step6_error TEXT;")

    # Step 7 tracking
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS step7_complete BOOLEAN NOT NULL DEFAULT FALSE;")
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS step7_completed_at TIMESTAMP WITH TIME ZONE;")
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS step7_smtp_auth_enabled BOOLEAN NOT NULL DEFAULT FALSE;")
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS step7_app_consent_granted BOOLEAN NOT NULL DEFAULT FALSE;")
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS step7_app_consent_granted_at TIMESTAMP WITH TIME ZONE;")
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS step7_app_consent_error TEXT;")
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS step7_error TEXT;")

    # Retry counts
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS step4_retry_count INTEGER NOT NULL DEFAULT 0;")
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS step5_retry_count INTEGER NOT NULL DEFAULT 0;")
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS step6_retry_count INTEGER NOT NULL DEFAULT 0;")
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS step7_retry_count INTEGER NOT NULL DEFAULT 0;")

    # Setup tracking
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS setup_error TEXT;")

    # ================================================================
    # DOMAINS TABLE — missing columns
    # ================================================================

    # Redirect URL
    op.execute("ALTER TABLE domains ADD COLUMN IF NOT EXISTS redirect_url VARCHAR(500);")
    op.execute("ALTER TABLE domains ADD COLUMN IF NOT EXISTS redirect_configured BOOLEAN NOT NULL DEFAULT FALSE;")

    # Multi-domain per tenant
    op.execute("ALTER TABLE domains ADD COLUMN IF NOT EXISTS domain_index_in_tenant INTEGER NOT NULL DEFAULT 0;")

    # M365 domain setup (per-domain)
    op.execute("ALTER TABLE domains ADD COLUMN IF NOT EXISTS domain_added_to_m365 BOOLEAN NOT NULL DEFAULT FALSE;")
    op.execute("ALTER TABLE domains ADD COLUMN IF NOT EXISTS m365_verification_txt VARCHAR(500);")
    op.execute("ALTER TABLE domains ADD COLUMN IF NOT EXISTS domain_verified_in_m365 BOOLEAN NOT NULL DEFAULT FALSE;")
    op.execute("ALTER TABLE domains ADD COLUMN IF NOT EXISTS domain_verified_at TIMESTAMP WITH TIME ZONE;")

    # DNS tracking (per-domain)
    op.execute("ALTER TABLE domains ADD COLUMN IF NOT EXISTS mx_record_added BOOLEAN NOT NULL DEFAULT FALSE;")
    op.execute("ALTER TABLE domains ADD COLUMN IF NOT EXISTS spf_record_added BOOLEAN NOT NULL DEFAULT FALSE;")
    op.execute("ALTER TABLE domains ADD COLUMN IF NOT EXISTS autodiscover_added BOOLEAN NOT NULL DEFAULT FALSE;")
    op.execute("ALTER TABLE domains ADD COLUMN IF NOT EXISTS mx_value VARCHAR(500);")
    op.execute("ALTER TABLE domains ADD COLUMN IF NOT EXISTS spf_value VARCHAR(500);")

    # DKIM tracking (per-domain)
    op.execute("ALTER TABLE domains ADD COLUMN IF NOT EXISTS dkim_selector1 VARCHAR(500);")
    op.execute("ALTER TABLE domains ADD COLUMN IF NOT EXISTS dkim_selector2 VARCHAR(500);")
    op.execute("ALTER TABLE domains ADD COLUMN IF NOT EXISTS dkim_enabled_at TIMESTAMP WITH TIME ZONE;")

    # Step 5 tracking (per-domain)
    op.execute("ALTER TABLE domains ADD COLUMN IF NOT EXISTS step5_complete BOOLEAN NOT NULL DEFAULT FALSE;")
    op.execute("ALTER TABLE domains ADD COLUMN IF NOT EXISTS step5_retry_count INTEGER NOT NULL DEFAULT 0;")

    # Licensed user (per-domain)
    op.execute("ALTER TABLE domains ADD COLUMN IF NOT EXISTS licensed_user_upn VARCHAR(255);")
    op.execute("ALTER TABLE domains ADD COLUMN IF NOT EXISTS licensed_user_password VARCHAR(255);")
    op.execute("ALTER TABLE domains ADD COLUMN IF NOT EXISTS licensed_user_created BOOLEAN NOT NULL DEFAULT FALSE;")
    op.execute("ALTER TABLE domains ADD COLUMN IF NOT EXISTS licensed_user_id VARCHAR(255);")

    # Skip flags (per-domain) — set when MAX_PIPELINE_RETRIES exceeded
    op.execute("ALTER TABLE domains ADD COLUMN IF NOT EXISTS step5_skipped BOOLEAN DEFAULT FALSE;")
    op.execute("ALTER TABLE domains ADD COLUMN IF NOT EXISTS step6_skipped BOOLEAN DEFAULT FALSE;")

    # Step 6 tracking (per-domain)
    op.execute("ALTER TABLE domains ADD COLUMN IF NOT EXISTS step6_complete BOOLEAN NOT NULL DEFAULT FALSE;")
    op.execute("ALTER TABLE domains ADD COLUMN IF NOT EXISTS step6_mailboxes_created INTEGER NOT NULL DEFAULT 0;")

    # ================================================================
    # MAILBOXES TABLE — missing columns
    # ================================================================

    op.execute("ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS local_part VARCHAR(255);")
    op.execute("ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS microsoft_object_id VARCHAR(255);")
    op.execute("ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS upn VARCHAR(255);")
    op.execute("ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS created_in_exchange BOOLEAN NOT NULL DEFAULT FALSE;")
    op.execute("ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS created_at_exchange TIMESTAMP WITH TIME ZONE;")
    op.execute("ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS display_name_fixed BOOLEAN NOT NULL DEFAULT FALSE;")
    op.execute("ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS photo_set BOOLEAN NOT NULL DEFAULT FALSE;")
    op.execute("ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS setup_complete BOOLEAN NOT NULL DEFAULT FALSE;")
    op.execute("ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS setup_completed_at TIMESTAMP WITH TIME ZONE;")
    op.execute("ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS uploaded_to_sequencer BOOLEAN NOT NULL DEFAULT FALSE;")
    op.execute("ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS uploaded_at TIMESTAMP WITH TIME ZONE;")
    op.execute("ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS sequencer_name VARCHAR(100);")
    op.execute("ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS upload_error TEXT;")
    op.execute("ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS error_message TEXT;")
    op.execute("ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS instantly_uploaded BOOLEAN NOT NULL DEFAULT FALSE;")
    op.execute("ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS instantly_uploaded_at TIMESTAMP WITH TIME ZONE;")
    op.execute("ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS instantly_upload_error TEXT;")
    op.execute("ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS smartlead_uploaded BOOLEAN NOT NULL DEFAULT FALSE;")
    op.execute("ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS smartlead_uploaded_at TIMESTAMP WITH TIME ZONE;")
    op.execute("ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS smartlead_upload_error TEXT;")
    op.execute("ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS initial_password VARCHAR(255);")

    # Make password nullable (it was NOT NULL in migration 001)
    op.execute("ALTER TABLE mailboxes ALTER COLUMN password DROP NOT NULL;")

    # ================================================================
    # PIPELINE_LOGS TABLE — create if not exists
    # ================================================================

    op.execute("""
        CREATE TABLE IF NOT EXISTS pipeline_logs (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            batch_id UUID NOT NULL REFERENCES setup_batches(id) ON DELETE CASCADE,
            step INTEGER NOT NULL,
            step_name VARCHAR NOT NULL,
            item_type VARCHAR,
            item_id VARCHAR,
            item_name VARCHAR,
            status VARCHAR NOT NULL,
            message VARCHAR,
            error_detail TEXT,
            retryable BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
        );
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_pipeline_logs_batch_id ON pipeline_logs(batch_id);")

    # ================================================================
    # INSTANTLY_ACCOUNTS TABLE — create if not exists
    # ================================================================

    op.execute("""
        CREATE TABLE IF NOT EXISTS instantly_accounts (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            email VARCHAR(255) NOT NULL UNIQUE,
            api_key VARCHAR(500),
            status VARCHAR(50) NOT NULL DEFAULT 'active',
            total_accounts INTEGER NOT NULL DEFAULT 0,
            warmup_enabled BOOLEAN NOT NULL DEFAULT FALSE,
            campaign_id VARCHAR(255),
            notes TEXT,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
        );
    """)


def downgrade() -> None:
    # Only drop columns that were added in THIS migration.
    # Using IF EXISTS so partial rollbacks are safe.
    # NOTE: Not dropping enum values (PostgreSQL limitation).

    # Pipeline logs table
    op.execute("DROP TABLE IF EXISTS pipeline_logs;")
    op.execute("DROP TABLE IF EXISTS instantly_accounts;")

    # Mailboxes — drop new columns
    for col in [
        'local_part', 'microsoft_object_id', 'upn', 'created_in_exchange',
        'created_at_exchange', 'display_name_fixed', 'photo_set',
        'setup_complete', 'setup_completed_at', 'uploaded_to_sequencer',
        'uploaded_at', 'sequencer_name', 'upload_error', 'error_message',
        'instantly_uploaded', 'instantly_uploaded_at', 'instantly_upload_error',
        'smartlead_uploaded', 'smartlead_uploaded_at', 'smartlead_upload_error',
        'initial_password',
    ]:
        op.execute(f"ALTER TABLE mailboxes DROP COLUMN IF EXISTS {col};")

    # Domains — drop new columns
    for col in [
        'redirect_url', 'redirect_configured', 'domain_index_in_tenant',
        'domain_added_to_m365', 'm365_verification_txt', 'domain_verified_in_m365',
        'domain_verified_at', 'mx_record_added', 'spf_record_added',
        'autodiscover_added', 'mx_value', 'spf_value', 'dkim_selector1',
        'dkim_selector2', 'dkim_enabled_at', 'step5_complete', 'step5_retry_count',
        'licensed_user_upn', 'licensed_user_password', 'licensed_user_created',
        'licensed_user_id', 'step6_complete', 'step6_mailboxes_created',
    ]:
        op.execute(f"ALTER TABLE domains DROP COLUMN IF EXISTS {col};")

    # Tenants — drop new columns
    for col in [
        'initial_password', 'totp_secret', 'address', 'contact_name',
        'contact_email', 'contact_phone', 'first_login_completed', 'first_login_at',
        'password_changed', 'security_defaults_disabled', 'security_defaults_error',
        'security_defaults_disabled_at', 'licensed_user_upn', 'licensed_user_password',
        'licensed_user_created', 'license_assigned', 'custom_domain',
        'domain_added_to_m365', 'm365_verification_txt', 'domain_verified_in_m365',
        'domain_verified_at', 'mx_record_added', 'spf_record_added',
        'autodiscover_added', 'dkim_selector1', 'dkim_selector2',
        'dkim_cnames_added', 'dkim_enabled', 'dkim_enabled_at',
        'dkim_retry_count', 'dkim_last_retry_at', 'mailboxes_generated',
        'mailboxes_created', 'mailboxes_created_at', 'delegation_completed',
        'mailbox_count', 'step5_complete', 'step5_completed_at',
        'step6_started', 'step6_started_at', 'step6_mailboxes_created',
        'step6_display_names_fixed', 'step6_accounts_enabled', 'step6_passwords_set',
        'step6_upns_fixed', 'step6_delegations_done', 'step6_complete',
        'step6_completed_at', 'step6_error', 'step7_complete', 'step7_completed_at',
        'step7_smtp_auth_enabled', 'step7_app_consent_granted',
        'step7_app_consent_granted_at', 'step7_app_consent_error', 'step7_error',
        'step4_retry_count', 'step5_retry_count', 'step6_retry_count',
        'step7_retry_count', 'setup_error',
    ]:
        op.execute(f"ALTER TABLE tenants DROP COLUMN IF EXISTS {col};")

    # Setup batches — drop new columns
    for col in [
        'domains_per_tenant', 'sequencer_app_key', 'completed_steps',
        'uploaded_to_sequencer', 'uploaded_at', 'auto_progress_enabled',
        'auto_run_state', 'step6_emails_generated', 'step6_emails_generated_at',
        'custom_mailbox_map', 'new_admin_password', 'sequencer_platform',
        'sequencer_login_email', 'sequencer_login_password', 'profile_photo_path',
        'pipeline_status', 'pipeline_step', 'pipeline_step_name',
        'pipeline_started_at', 'pipeline_completed_at', 'pipeline_paused_at',
        'ns_confirmed_at', 'total_domains', 'total_tenants', 'zones_completed',
        'ns_propagated_count', 'dns_completed', 'first_login_completed_count',
        'm365_completed', 'mailboxes_completed_count', 'smtp_completed',
        'sequencer_uploaded_count', 'errors_count',
    ]:
        op.execute(f"ALTER TABLE setup_batches DROP COLUMN IF EXISTS {col};")
