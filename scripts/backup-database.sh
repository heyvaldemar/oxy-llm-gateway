#!/bin/bash
# ============================================================================
# AUTOMATED DATABASE BACKUPS
# ============================================================================
# Purpose: Daily Supabase PostgreSQL backups to S3
# Schedule: Run daily via cron (0 2 * * *)
# Retention: 90 days
# Use: disaster recovery
# ============================================================================

set -e  # Exit on error
set -u  # Error on undefined variables

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Configuration
TIMESTAMP=$(date +%Y-%m-%d_%H-%M-%S)
BACKUP_DIR="/tmp/oxy-backups"
BACKUP_FILE="oxy-backup-${TIMESTAMP}.sql.gz"
S3_BUCKET="${S3_BACKUP_BUCKET:-oxy-backups}"
S3_PREFIX="daily"
RETENTION_DAYS=90

# Supabase configuration (from environment)
DB_HOST="${DB_HOST:-db.your-project.supabase.co}"
DB_PORT="${DB_PORT:-6543}"
DB_NAME="${DB_NAME:-postgres}"
DB_USER="${DB_USER:-postgres}"
DB_PASSWORD="${DB_PASSWORD}"

# Check required variables
if [ -z "${DB_PASSWORD:-}" ]; then
    echo -e "${RED}❌ ERROR: DB_PASSWORD not set${NC}"
    exit 1
fi

# Create backup directory
mkdir -p "$BACKUP_DIR"

echo -e "${GREEN}🗄️  Starting database backup...${NC}"
echo "================================================"
echo "Timestamp: $TIMESTAMP"
echo "Database: $DB_HOST:$DB_PORT/$DB_NAME"
echo "S3 Bucket: s3://$S3_BUCKET/$S3_PREFIX/"
echo "================================================"

# Step 1: Dump database
echo -e "\n${YELLOW}Step 1/4: Dumping database...${NC}"
PGPASSWORD="$DB_PASSWORD" pg_dump \
    -h "$DB_HOST" \
    -p "$DB_PORT" \
    -U "$DB_USER" \
    -d "$DB_NAME" \
    --no-owner \
    --no-acl \
    --clean \
    --if-exists \
    | gzip > "$BACKUP_DIR/$BACKUP_FILE"

BACKUP_SIZE=$(du -h "$BACKUP_DIR/$BACKUP_FILE" | cut -f1)
echo -e "${GREEN}✓ Database dumped (${BACKUP_SIZE})${NC}"

# Step 2: Upload to S3
echo -e "\n${YELLOW}Step 2/4: Uploading to S3...${NC}"
aws s3 cp "$BACKUP_DIR/$BACKUP_FILE" "s3://$S3_BUCKET/$S3_PREFIX/" --storage-class STANDARD_IA

if [ $? -eq 0 ]; then
    echo -e "${GREEN}✓ Uploaded to S3${NC}"
else
    echo -e "${RED}✗ S3 upload failed${NC}"
    exit 1
fi

# Step 3: Verify backup
echo -e "\n${YELLOW}Step 3/4: Verifying backup...${NC}"
aws s3 ls "s3://$S3_BUCKET/$S3_PREFIX/$BACKUP_FILE"

if [ $? -eq 0 ]; then
    echo -e "${GREEN}✓ Backup verified on S3${NC}"
else
    echo -e "${RED}✗ Backup verification failed${NC}"
    exit 1
fi

# Step 4: Clean up old backups
echo -e "\n${YELLOW}Step 4/4: Cleaning up old backups...${NC}"

# Delete local backup
rm -f "$BACKUP_DIR/$BACKUP_FILE"
echo -e "${GREEN}✓ Local backup cleaned up${NC}"

# Delete S3 backups older than retention period
CUTOFF_DATE=$(date -d "-${RETENTION_DAYS} days" +%Y-%m-%d)
echo "Deleting backups older than $CUTOFF_DATE..."

aws s3 ls "s3://$S3_BUCKET/$S3_PREFIX/" | while read -r line; do
    BACKUP_DATE=$(echo "$line" | awk '{print $1}')
    BACKUP_NAME=$(echo "$line" | awk '{print $4}')
    
    if [[ "$BACKUP_DATE" < "$CUTOFF_DATE" ]]; then
        echo "Deleting old backup: $BACKUP_NAME"
        aws s3 rm "s3://$S3_BUCKET/$S3_PREFIX/$BACKUP_NAME"
    fi
done

echo -e "${GREEN}✓ Old backups cleaned up${NC}"

# Summary
echo ""
echo "================================================"
echo -e "${GREEN}✅ Backup completed successfully!${NC}"
echo "================================================"
echo "Backup file: $BACKUP_FILE"
echo "Backup size: $BACKUP_SIZE"
echo "S3 location: s3://$S3_BUCKET/$S3_PREFIX/$BACKUP_FILE"
echo "Retention: $RETENTION_DAYS days"
echo ""
echo "To restore:"
echo "  aws s3 cp s3://$S3_BUCKET/$S3_PREFIX/$BACKUP_FILE - | gunzip | psql"
echo "  (psql connects with PGHOST, PGUSER, PGPASSWORD and PGDATABASE from the environment,"
echo "   so the password is not on its command line)"
echo "================================================"

# Send notification (optional)
if [ -n "${SLACK_WEBHOOK_URL:-}" ]; then
    # The webhook URL is its own credential: curl reads it on standard input.
    printf 'url = "%s"\n' "$SLACK_WEBHOOK_URL" | curl -sS -X POST --config - \
        -H 'Content-Type: application/json' \
        -d "{\"text\":\"✅ Database backup completed: ${BACKUP_FILE} (${BACKUP_SIZE})\"}"
fi

exit 0

