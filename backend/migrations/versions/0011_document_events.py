"""资料与任务提交后发送所属用户的失效通知。"""

from alembic import op

revision = "0011_document_events"
down_revision = "0010_ragflow_lexical_rev"
branch_labels = None
depends_on = None

INSTALL_SQL = """
CREATE OR REPLACE FUNCTION ks_notify_document_change() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    owner text;
    resource uuid;
BEGIN
    IF TG_TABLE_NAME = 'knowledge_bases' THEN
        IF TG_OP = 'DELETE' THEN owner := OLD.owner_id;
        ELSE owner := NEW.owner_id; END IF;
    ELSIF TG_TABLE_NAME = 'documents' THEN
        IF TG_OP = 'DELETE' THEN resource := OLD.kb_id;
        ELSE resource := NEW.kb_id; END IF;
        SELECT owner_id INTO owner FROM knowledge_bases WHERE id = resource;
    ELSE
        IF TG_OP = 'DELETE' THEN resource := OLD.resource_id;
        ELSE resource := NEW.resource_id; END IF;
        SELECT kb.owner_id INTO owner FROM documents d
        JOIN knowledge_bases kb ON kb.id = d.kb_id WHERE d.id = resource;
    END IF;
    IF owner IS NOT NULL THEN
        PERFORM pg_notify('ks_changes_' || md5(owner), '');
    END IF;
    RETURN NULL;
END;
$$;
CREATE TRIGGER ks_kb_changes AFTER INSERT OR UPDATE OR DELETE ON knowledge_bases
FOR EACH ROW EXECUTE FUNCTION ks_notify_document_change();
CREATE TRIGGER ks_document_changes AFTER INSERT OR UPDATE OR DELETE ON documents
FOR EACH ROW EXECUTE FUNCTION ks_notify_document_change();
CREATE TRIGGER ks_job_created AFTER INSERT ON jobs
FOR EACH ROW EXECUTE FUNCTION ks_notify_document_change();
CREATE TRIGGER ks_job_changes AFTER UPDATE OF state, stage, attempt, last_error ON jobs
FOR EACH ROW WHEN (
    (OLD.state, OLD.stage, OLD.attempt, OLD.last_error) IS DISTINCT FROM
    (NEW.state, NEW.stage, NEW.attempt, NEW.last_error)
) EXECUTE FUNCTION ks_notify_document_change();
"""


def upgrade() -> None:
    op.execute(INSTALL_SQL)


def downgrade() -> None:
    op.execute("DROP TRIGGER ks_job_changes ON jobs")
    op.execute("DROP TRIGGER ks_job_created ON jobs")
    op.execute("DROP TRIGGER ks_document_changes ON documents")
    op.execute("DROP TRIGGER ks_kb_changes ON knowledge_bases")
    op.execute("DROP FUNCTION ks_notify_document_change()")
