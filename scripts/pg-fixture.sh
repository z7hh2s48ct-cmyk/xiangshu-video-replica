#!/bin/bash
# T03 - PostgreSQL Fixture Management Script
# This script manages the PostgreSQL test container for T03 milestone

set -e

# Parameterized for parallel CI shards (each shard gets its own isolated
# container + port). Defaults are unchanged, so `scripts/pg-fixture.sh start`
# behaves exactly as before for local development.
CONTAINER_NAME="${PG_FIXTURE_NAME:-customer-v3-pg-test}"
# Volume derives from the container name so parallel fixtures never share data,
# but the historical default volume name is preserved for backward compat.
if [ -n "${PG_FIXTURE_VOLUME:-}" ]; then
    DATA_VOLUME="${PG_FIXTURE_VOLUME}"
elif [ "${CONTAINER_NAME}" = "customer-v3-pg-test" ]; then
    DATA_VOLUME="customer-v3-pg-data"
else
    DATA_VOLUME="${CONTAINER_NAME}-data"
fi
HOST_PORT="${PG_FIXTURE_PORT:-5433}"
DB_NAME="${PG_FIXTURE_DB:-customer_v3_test}"
CONTAINER_PORT=5432
PG_USER=testuser
PG_PASSWORD=testpass

usage() {
    echo "Usage: $0 {start|stop|clean|status|test}"
    echo "Commands:"
    echo "  start   - Start PostgreSQL container (default)"
    echo "  stop    - Stop and remove container"
    echo "  clean   - Remove data volume as well"
    echo "  status  - Show container status"
    echo "  test    - Run pytest against the fixture"
    exit 1
}

case "$1" in
    start)
        echo "Starting PostgreSQL 16 fixture..."
        
        # Check if already running
        if docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
            echo "Container ${CONTAINER_NAME} is already running"
            docker logs $CONTAINER_NAME 2>&1 | tail -5
            exit 0
        fi
        
        # Remove old container if exists
        if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
            echo "Removing stopped container..."
            docker rm $CONTAINER_NAME || true
        fi
        
        # Create volume if not exists
        if ! docker volume ls --format '{{.Name}}' | grep -q "^${DATA_VOLUME}$"; then
            echo "Creating volume ${DATA_VOLUME}..."
            docker volume create $DATA_VOLUME
        fi
        
        # Start fresh container
        echo "Launching postgres:16-alpine container..."
        docker run -d \
            --name ${CONTAINER_NAME} \
            -e POSTGRES_USER=${PG_USER} \
            -e POSTGRES_PASSWORD=${PG_PASSWORD} \
            -e POSTGRES_DB=${DB_NAME} \
            -p ${HOST_PORT}:${CONTAINER_PORT} \
            -v ${DATA_VOLUME}:/var/lib/postgresql/data \
            postgres:16-alpine
        
        # Wait for startup
        echo "Waiting for PostgreSQL to be ready..."
        for i in {1..30}; do
            if docker exec $CONTAINER_NAME pg_isready -U ${PG_USER} -d ${DB_NAME} > /dev/null 2>&1; then
                echo "PostgreSQL is ready!"
                docker logs $CONTAINER_NAME 2>&1 | tail -5
                exit 0
            fi
            sleep 1
        done
        
        echo "ERROR: PostgreSQL failed to start within 30 seconds"
        docker logs $CONTAINER_NAME 2>&1 | tail -10
        exit 1
        ;;
        
    stop)
        echo "Stopping PostgreSQL container..."
        if docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
            docker stop $CONTAINER_NAME
            docker rm $CONTAINER_NAME
            echo "Container stopped and removed"
        else
            echo "Container ${CONTAINER_NAME} is not running"
        fi
        ;;
        
    clean)
        echo "Cleaning up PostgreSQL fixture completely..."
        docker stop $CONTAINER_NAME 2>/dev/null || true
        docker rm $CONTAINER_NAME 2>/dev/null || true
        docker volume rm $DATA_VOLUME 2>/dev/null || true
        echo "All resources cleaned"
        ;;
        
    status)
        echo "=== Container Status ==="
        docker ps --filter "name=${CONTAINER_NAME}" || echo "Container not running"
        echo ""
        echo "=== Volume Status ==="
        docker volume ls --filter "name=${DATA_VOLUME}" || echo "Volume not found"
        echo ""
        echo "=== Test DSN ==="
        echo "postgresql://${PG_USER}:${PG_PASSWORD}@localhost:${HOST_PORT}/${DB_NAME}"
        export TEST_POSTGRESQL_URL="postgresql://${PG_USER}:${PG_PASSWORD}@localhost:${HOST_PORT}/${DB_NAME}"
        echo "Set environment: TEST_POSTGRESQL_URL=$TEST_POSTGRESQL_URL"
        ;;
        
    test)
        # Ensure container is running
        if ! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
            echo "Error: Container not running. Run '$0 start' first."
            exit 1
        fi
        
        echo "Running PostgreSQL fixture tests..."
        export TEST_POSTGRESQL_URL="postgresql://${PG_USER}:${PG_PASSWORD}@localhost:${HOST_PORT}/${DB_NAME}"
        cd server
        uv run python -m pytest tests/test_postgres_migrations.py -v
        ;;
        
    *)
        usage
        ;;
esac
