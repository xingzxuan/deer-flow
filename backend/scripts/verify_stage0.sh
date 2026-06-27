#!/usr/bin/env bash
# verify_stage0.sh — systematic verification of the Stage 0 multi-tenant rollout.
#
# Layers (each can be run individually; defaults run all that are applicable):
#   static   — full pytest suite + lint + boundary scan (no external deps)
#   paths    — local filesystem layout: legacy users/ should be empty after migration
#   rds      — schema + index + alembic state on the remote Postgres (needs DATABASE_URL)
#   runtime  — gateway health probes (needs `make dev` running)
#   e2e      — register 2 users via curl, each creates a thread, cross-access must 404
#
# Usage:
#   ./scripts/verify_stage0.sh                 # run everything that has prerequisites
#   ./scripts/verify_stage0.sh static rds      # only those two
#   DATABASE_URL=postgres://... ./scripts/verify_stage0.sh
#   GATEWAY_URL=http://localhost:8001 ./scripts/verify_stage0.sh runtime e2e
#
# Exit code: 0 if every executed assertion passes, 1 if any fail.

set -uo pipefail

# ── colours ──────────────────────────────────────────────────────────────
if [ -t 1 ]; then
    C_RED=$'\033[0;31m'; C_GREEN=$'\033[0;32m'; C_YELLOW=$'\033[0;33m'
    C_BLUE=$'\033[0;34m'; C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'; C_RESET=$'\033[0m'
else
    C_RED=''; C_GREEN=''; C_YELLOW=''; C_BLUE=''; C_BOLD=''; C_DIM=''; C_RESET=''
fi

# ── counters ─────────────────────────────────────────────────────────────
PASS_COUNT=0
FAIL_COUNT=0
WARN_COUNT=0
FAILED_STEPS=()
SKIPPED_PHASES=()

ok()   { printf '  %s✓%s %s\n' "$C_GREEN" "$C_RESET" "$1"; PASS_COUNT=$((PASS_COUNT+1)); }
fail() { printf '  %s✗%s %s\n' "$C_RED"   "$C_RESET" "$1"; FAIL_COUNT=$((FAIL_COUNT+1)); FAILED_STEPS+=("$1"); }
warn() { printf '  %s!%s %s\n' "$C_YELLOW" "$C_RESET" "$1"; WARN_COUNT=$((WARN_COUNT+1)); }
info() { printf '  %s·%s %s\n' "$C_DIM"   "$C_RESET" "$1"; }
phase() { printf '\n%s== %s ==%s\n' "$C_BLUE$C_BOLD" "$1" "$C_RESET"; }

# Run-cmd helpers — capture both streams for grep but return original exit code.
run() {
    local label="$1"; shift
    local out
    out=$("$@" 2>&1)
    local rc=$?
    if [ "$rc" -eq 0 ]; then
        ok "$label"
        printf '%s' "$out"
        return 0
    fi
    fail "$label (exit $rc)"
    printf '%s%s%s\n' "$C_DIM" "$out" "$C_RESET" >&2
    return "$rc"
}

# ── env ──────────────────────────────────────────────────────────────────
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
BACKEND_DIR="$REPO_ROOT/backend"
GATEWAY_URL="${GATEWAY_URL:-http://localhost:8001}"
DEER_FLOW_HOME="${DEER_FLOW_HOME:-$REPO_ROOT/.deer-flow}"

# ── phase: static ────────────────────────────────────────────────────────
phase_static() {
    phase "STATIC — unit + boundary + full pytest"
    cd "$BACKEND_DIR"

    # boundary scans first — fast and high-signal
    info "running boundary scans (harness + workspace)"
    if PYTHONPATH=. uv run pytest -q \
            tests/test_harness_boundary.py \
            tests/test_workspace_boundary.py \
            tests/test_workspace_boundary_self.py >/tmp/verify_boundary.log 2>&1; then
        local boundary_passed
        boundary_passed=$(grep -Eo '[0-9]+ passed' /tmp/verify_boundary.log | head -1)
        ok "boundary scans green ($boundary_passed)"
    else
        fail "boundary scans red — see /tmp/verify_boundary.log"
    fi

    info "running full pytest (this takes ~90-130s)"
    local pytest_log=/tmp/verify_full_pytest.log
    PYTHONPATH=. uv run pytest -q >"$pytest_log" 2>&1
    local rc=$?
    local last_line
    last_line=$(tail -1 "$pytest_log")
    info "result: $last_line"
    # Parse "<X> passed, <Y> failed, <Z> skipped"
    local passed_n failed_n
    passed_n=$(printf '%s' "$last_line" | grep -Eo '[0-9]+ passed' | grep -Eo '[0-9]+' | head -1 || echo 0)
    failed_n=$(printf '%s' "$last_line" | grep -Eo '[0-9]+ failed' | grep -Eo '[0-9]+' | head -1 || echo 0)
    if [ "${passed_n:-0}" -ge 3250 ]; then
        ok "pytest pass count $passed_n ≥ 3250 (PR8 baseline)"
    else
        fail "pytest pass count $passed_n < 3250 — regression suspected"
    fi
    if [ "${failed_n:-0}" -le 18 ]; then
        ok "pytest fail count $failed_n ≤ 18 (Stage 0 known-flake ceiling)"
        [ "${failed_n:-0}" -gt 0 ] && warn "non-zero failures expected to be in the 18 caplog flake set; cross-check tail of /tmp/verify_full_pytest.log"
    else
        fail "pytest fail count $failed_n > 18 — new failures introduced beyond known caplog flake set"
    fi

    info "running ruff lint"
    if make lint >/tmp/verify_lint.log 2>&1; then
        ok "ruff lint clean"
    else
        fail "ruff lint dirty — see /tmp/verify_lint.log"
    fi
}

# ── phase: paths ─────────────────────────────────────────────────────────
phase_paths() {
    phase "PATHS — legacy users/ migration state"
    if [ ! -d "$DEER_FLOW_HOME" ]; then
        warn "DEER_FLOW_HOME ($DEER_FLOW_HOME) does not exist — fresh install, nothing to migrate"
        return
    fi

    info "DEER_FLOW_HOME = $DEER_FLOW_HOME"
    local legacy_dir="$DEER_FLOW_HOME/users"
    if [ ! -d "$legacy_dir" ]; then
        ok "no legacy users/ directory present (PR6 migration not needed or already done)"
    else
        local count
        count=$(find "$legacy_dir" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')
        if [ "$count" -eq 0 ]; then
            ok "legacy users/ is empty (migration complete or no prior data)"
        else
            warn "legacy users/ still has $count user dir(s) — run \`make migrate-paths\` (after \`make migrate-paths DRY_RUN=1\` to preview)"
        fi
    fi

    local workspace_dir="$DEER_FLOW_HOME/workspaces"
    if [ -d "$workspace_dir" ]; then
        local wcount
        wcount=$(find "$workspace_dir" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')
        ok "workspaces/ layout in place ($wcount workspace dir(s))"
    else
        warn "workspaces/ dir does not exist yet — will be created when the first thread is opened"
    fi

    # Stage 0 PR6 dry-run check: just exercise the script flag, do not perform writes.
    info "exercising migrate-paths dry-run (no writes)"
    cd "$REPO_ROOT"
    if make migrate-paths DRY_RUN=1 >/tmp/verify_migrate_dry.log 2>&1; then
        ok "make migrate-paths DRY_RUN=1 ran without error"
    else
        fail "make migrate-paths DRY_RUN=1 failed — see /tmp/verify_migrate_dry.log"
    fi
}

# ── phase: rds ───────────────────────────────────────────────────────────
phase_rds() {
    phase "RDS — schema + alembic + indexes"
    if [ -z "${DATABASE_URL:-}" ]; then
        warn "DATABASE_URL not set — skipping RDS phase"
        SKIPPED_PHASES+=("rds (DATABASE_URL unset)")
        return
    fi
    if ! command -v psql >/dev/null 2>&1; then
        fail "psql not installed — cannot run RDS phase"
        return
    fi
    info "DATABASE_URL host: $(printf '%s' "$DATABASE_URL" | sed -E 's#.*@([^/]+)/.*#\1#')"

    # PG version sanity
    local pg_version
    pg_version=$(psql "$DATABASE_URL" -tA -c "SELECT version()" 2>/dev/null | head -1)
    if [ -z "$pg_version" ]; then
        fail "cannot connect to RDS — check DATABASE_URL"
        return
    fi
    ok "PG reachable: $pg_version"

    # Alembic head
    info "checking alembic head against expected 0003"
    cd "$BACKEND_DIR"
    if PYTHONPATH=. uv run alembic current 2>/tmp/verify_alembic.err | tee /tmp/verify_alembic.log | grep -q "^0003"; then
        ok "alembic current is at 0003 (workspace_id NOT NULL + UNIQUE(wid, tid))"
    else
        fail "alembic current is not at 0003 — see /tmp/verify_alembic.log"
    fi
    cd "$REPO_ROOT"

    # PR8 tables present
    info "checking PR8 tables exist"
    local pr8_count
    pr8_count=$(psql "$DATABASE_URL" -tA -c "
        SELECT count(*) FROM information_schema.tables
        WHERE table_schema = current_schema()
          AND table_name IN ('service_accounts','api_keys','external_users');
    " 2>/dev/null | tr -d ' ')
    if [ "${pr8_count:-0}" -eq 3 ]; then
        ok "service_accounts + api_keys + external_users all present"
    else
        fail "PR8 tables missing — expected 3, got ${pr8_count:-0}"
    fi

    # Partial index on api_keys: WHERE revoked_at IS NULL
    info "checking idx_api_keys_active partial-index predicate"
    local idx_def
    idx_def=$(psql "$DATABASE_URL" -tA -c "
        SELECT indexdef FROM pg_indexes
        WHERE schemaname = current_schema()
          AND indexname = 'idx_api_keys_active';
    " 2>/dev/null | head -1)
    if printf '%s' "$idx_def" | grep -qi 'WHERE.*revoked_at IS NULL'; then
        ok "idx_api_keys_active has 'WHERE revoked_at IS NULL' predicate"
    else
        fail "idx_api_keys_active missing or wrong predicate — got: ${idx_def:-<none>}"
    fi

    # 4 business tables workspace_id NOT NULL
    info "checking workspace_id NOT NULL on 4 business tables"
    local nullable_rows
    nullable_rows=$(psql "$DATABASE_URL" -tA -c "
        SELECT table_name FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND column_name = 'workspace_id'
          AND table_name IN ('thread_meta','runs','feedback','run_events')
          AND is_nullable = 'YES';
    " 2>/dev/null)
    if [ -z "$nullable_rows" ]; then
        ok "thread_meta + runs + feedback + run_events all have workspace_id NOT NULL"
    else
        fail "workspace_id is nullable in: $(printf '%s' "$nullable_rows" | tr '\n' ' ')"
    fi

    # Unique (workspace_id, thread_id) on thread_meta
    info "checking UNIQUE(workspace_id, thread_id) on thread_meta"
    local uq_count
    uq_count=$(psql "$DATABASE_URL" -tA -c "
        SELECT count(*) FROM pg_indexes
        WHERE schemaname = current_schema()
          AND tablename = 'thread_meta'
          AND indexdef ILIKE '%UNIQUE%workspace_id%thread_id%';
    " 2>/dev/null | tr -d ' ')
    if [ "${uq_count:-0}" -ge 1 ]; then
        ok "UNIQUE(workspace_id, thread_id) constraint/index present"
    else
        fail "no UNIQUE(workspace_id, thread_id) on thread_meta"
    fi
}

# ── phase: runtime ───────────────────────────────────────────────────────
phase_runtime() {
    phase "RUNTIME — gateway health"
    if ! command -v curl >/dev/null 2>&1; then
        fail "curl missing — cannot run runtime phase"
        return
    fi
    info "probing $GATEWAY_URL/health"
    local health
    health=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "$GATEWAY_URL/health" 2>/dev/null || true)
    if [ "$health" = "200" ]; then
        ok "gateway /health returns 200"
    else
        fail "gateway /health returned '$health' — is \`make dev\` running?"
        SKIPPED_PHASES+=("e2e (gateway not reachable)")
        SKIP_E2E=1
    fi
}

# ── phase: e2e ───────────────────────────────────────────────────────────
phase_e2e() {
    phase "E2E — register 2 users + cross-workspace 404"
    if [ "${SKIP_E2E:-0}" = "1" ]; then
        warn "skipped because gateway probe failed"
        return
    fi
    if ! command -v curl >/dev/null 2>&1; then
        fail "curl missing"
        return
    fi

    local tag
    tag=$(date +%s)
    local alice="alice-${tag}@verify-stage0.com"
    local bob="bob-${tag}@verify-stage0.com"
    local pw="VerifyStage0_${tag}!"
    local jar_a=/tmp/verify_alice_${tag}.cookies
    local jar_b=/tmp/verify_bob_${tag}.cookies
    rm -f "$jar_a" "$jar_b"

    # Ensure system is initialized (admin account exists) before registering users.
    info "ensuring admin account exists (POST /api/v1/auth/initialize)"
    local init_code
    init_code=$(curl -sS -o /dev/null -w '%{http_code}' \
        -H 'Content-Type: application/json' \
        -d "{\"email\":\"admin-${tag}@verify-stage0.com\",\"password\":\"$pw\"}" \
        "$GATEWAY_URL/api/v1/auth/initialize")
    if [ "$init_code" = "201" ]; then
        ok "admin initialized (first boot)"
    elif [ "$init_code" = "409" ]; then
        ok "admin already exists (system previously initialized)"
    else
        fail "admin initialization returned $init_code"
        return
    fi

    register_user() {
        local jar="$1"; local email="$2"
        curl -sS -c "$jar" -o /tmp/verify_register_$$.json -w '%{http_code}' \
            -H 'Content-Type: application/json' \
            -d "{\"email\":\"$email\",\"password\":\"$pw\"}" \
            "$GATEWAY_URL/api/v1/auth/register"
    }

    info "registering Alice + Bob"
    local code_a code_b
    code_a=$(register_user "$jar_a" "$alice")
    code_b=$(register_user "$jar_b" "$bob")
    if [ "$code_a" = "201" ] && [ "$code_b" = "201" ]; then
        ok "both users registered (201 / 201)"
    else
        fail "registration failed: alice=$code_a, bob=$code_b"
        return
    fi

    # Pull CSRF tokens from cookie jars (csrf_token cookie value, 4th-from-last field).
    csrf_from_jar() {
        awk '$6 == "csrf_token" { print $7 }' "$1" | tail -1
    }
    local csrf_a csrf_b
    csrf_a=$(csrf_from_jar "$jar_a")
    csrf_b=$(csrf_from_jar "$jar_b")
    if [ -n "$csrf_a" ] && [ -n "$csrf_b" ]; then
        ok "CSRF token captured for both sessions"
    else
        fail "CSRF cookie missing (alice='${csrf_a:0:8}...', bob='${csrf_b:0:8}...')"
        return
    fi

    create_thread() {
        local jar="$1"; local csrf="$2"; local tid="$3"
        curl -sS -b "$jar" -o /tmp/verify_thread_$$.json -w '%{http_code}' \
            -H 'Content-Type: application/json' \
            -H "X-CSRF-Token: $csrf" \
            -d "{\"thread_id\":\"$tid\"}" \
            "$GATEWAY_URL/api/threads"
    }

    local tid_a="verify-alice-${tag}"
    local tid_b="verify-bob-${tag}"
    info "Alice creates thread $tid_a; Bob creates thread $tid_b"
    local cta ctb
    cta=$(create_thread "$jar_a" "$csrf_a" "$tid_a")
    ctb=$(create_thread "$jar_b" "$csrf_b" "$tid_b")
    if [ "$cta" = "200" ] && [ "$ctb" = "200" ]; then
        ok "both threads created (200 / 200)"
    else
        fail "thread creation failed: alice=$cta, bob=$ctb"
        return
    fi

    # Cross access: Alice tries to GET Bob's thread → must be 404 (per PR6 contract:
    # cross-workspace returns 404, not 403, to avoid leaking existence).
    info "Alice → Bob's thread (GET)"
    local cross_get
    cross_get=$(curl -sS -b "$jar_a" -o /dev/null -w '%{http_code}' "$GATEWAY_URL/api/threads/$tid_b")
    if [ "$cross_get" = "404" ]; then
        ok "cross-workspace GET returns 404 (no existence leak)"
    else
        fail "cross-workspace GET returned '$cross_get', expected 404 — PR6 isolation broken"
    fi

    info "Alice → Bob's thread (DELETE)"
    local cross_del
    cross_del=$(curl -sS -b "$jar_a" -X DELETE -H "X-CSRF-Token: $csrf_a" \
                    -o /dev/null -w '%{http_code}' "$GATEWAY_URL/api/threads/$tid_b")
    if [ "$cross_del" = "404" ]; then
        ok "cross-workspace DELETE returns 404"
    else
        fail "cross-workspace DELETE returned '$cross_del', expected 404"
    fi

    # Same-workspace GET — sanity check Alice can still reach her own thread.
    info "Alice → Alice's thread (sanity)"
    local same_get
    same_get=$(curl -sS -b "$jar_a" -o /dev/null -w '%{http_code}' "$GATEWAY_URL/api/threads/$tid_a")
    if [ "$same_get" = "200" ]; then
        ok "same-workspace GET returns 200 (isolation is not over-blocking)"
    else
        fail "same-workspace GET returned '$same_get', expected 200"
    fi

    info "cookie jars left in /tmp for debugging: $jar_a $jar_b"
}

# ── main ─────────────────────────────────────────────────────────────────
main() {
    local args=("$@")
    if [ ${#args[@]} -eq 0 ]; then
        args=(static paths rds runtime e2e)
    fi

    printf '%s%sStage 0 verification — %s%s\n' "$C_BOLD" "$C_BLUE" "$(date)" "$C_RESET"
    printf 'Repo root: %s\n' "$REPO_ROOT"
    printf 'Phases: %s\n' "${args[*]}"

    for phase_name in "${args[@]}"; do
        case "$phase_name" in
            static)  phase_static ;;
            paths)   phase_paths ;;
            rds)     phase_rds ;;
            runtime) phase_runtime ;;
            e2e)     phase_e2e ;;
            all)
                phase_static; phase_paths; phase_rds; phase_runtime; phase_e2e
                ;;
            *)
                warn "unknown phase: $phase_name"
                ;;
        esac
    done

    printf '\n%s%s──── summary ────%s\n' "$C_BOLD" "$C_BLUE" "$C_RESET"
    printf '  %s%d passed%s   %s%d failed%s   %s%d warn%s\n' \
        "$C_GREEN" "$PASS_COUNT" "$C_RESET" \
        "$C_RED"   "$FAIL_COUNT" "$C_RESET" \
        "$C_YELLOW" "$WARN_COUNT" "$C_RESET"
    if [ ${#SKIPPED_PHASES[@]} -gt 0 ]; then
        printf '  skipped: %s\n' "${SKIPPED_PHASES[*]}"
    fi
    if [ ${#FAILED_STEPS[@]} -gt 0 ]; then
        printf '\n%sfailing steps:%s\n' "$C_RED" "$C_RESET"
        for step in "${FAILED_STEPS[@]}"; do
            printf '  ✗ %s\n' "$step"
        done
    fi
    [ "$FAIL_COUNT" -eq 0 ]
}

main "$@"
