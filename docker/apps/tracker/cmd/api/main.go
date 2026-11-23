// tracker-api serves every read and write of Tracker, an issue tracker used
// by many workspaces, and is its sync engine.
//
// Every change is written to the database together with a sync action: the
// kind of change, the entity after it, the fields that changed, and who made
// it. Users' apps keep a copy of their workspace and stay current by
// following those actions (/sync/delta).
//
// Entities are read through a cache in front of the database.
package main

import (
	"context"
	"crypto/rand"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"

	"tracker/internal/tracker"
)

// How long an entity stays in the cache.
const cacheTTL = 24 * time.Hour

var (
	uuidPattern = regexp.MustCompile(`^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$`)
	states      = map[string]bool{"backlog": true, "todo": true, "in_progress": true, "done": true, "canceled": true}
)

type Issue struct {
	ID          string    `json:"id"`
	WorkspaceID int       `json:"workspaceId"`
	TeamID      string    `json:"teamId"`
	Number      int       `json:"number"`
	Title       string    `json:"title"`
	Description string    `json:"description"`
	State       string    `json:"state"`
	Priority    int       `json:"priority"`
	AssigneeID  *string   `json:"assigneeId"`
	CreatorID   string    `json:"creatorId"`
	CreatedAt   time.Time `json:"createdAt"`
	UpdatedAt   time.Time `json:"updatedAt"`
	SyncID      int64     `json:"syncId"`
}

type Comment struct {
	ID          string    `json:"id"`
	WorkspaceID int       `json:"workspaceId"`
	IssueID     string    `json:"issueId"`
	UserID      string    `json:"userId"`
	Body        string    `json:"body"`
	CreatedAt   time.Time `json:"createdAt"`
	UpdatedAt   time.Time `json:"updatedAt"`
	SyncID      int64     `json:"syncId"`
}

type Action struct {
	ID          int64           `json:"id"`
	WorkspaceID int             `json:"workspaceId"`
	Model       string          `json:"model"`
	ModelID     string          `json:"modelId"`
	Action      string          `json:"action"`
	Data        json.RawMessage `json:"data"`
	Delta       json.RawMessage `json:"delta"`
	ActorID     string          `json:"actorId"`
	CreatedAt   time.Time       `json:"createdAt"`
}

type cachedIssue struct {
	value  Issue
	loaded time.Time
}

type cachedComment struct {
	value  Comment
	loaded time.Time
}

type server struct {
	db *pgxpool.Pool

	mu       sync.Mutex // guards the cache
	issues   map[string]cachedIssue
	comments map[string]cachedComment
	users    map[string]int // user -> workspace
	teams    map[string]int // team -> workspace

	locks       sync.Map // workspace -> *sync.Mutex: one write at a time in a workspace
	maintenance atomic.Bool
}

const (
	issueColumns   = "id, workspace_id, team_id, number, title, description, state, priority, assignee_id, creator_id, created_at, updated_at, sync_id"
	commentColumns = "id, workspace_id, issue_id, user_id, body, created_at, updated_at, sync_id"
)

func scanIssue(row pgx.Row) (Issue, error) {
	var i Issue
	err := row.Scan(&i.ID, &i.WorkspaceID, &i.TeamID, &i.Number, &i.Title, &i.Description, &i.State, &i.Priority,
		&i.AssigneeID, &i.CreatorID, &i.CreatedAt, &i.UpdatedAt, &i.SyncID)
	i.CreatedAt, i.UpdatedAt = i.CreatedAt.UTC(), i.UpdatedAt.UTC()
	return i, err
}

func scanComment(row pgx.Row) (Comment, error) {
	var c Comment
	err := row.Scan(&c.ID, &c.WorkspaceID, &c.IssueID, &c.UserID, &c.Body, &c.CreatedAt, &c.UpdatedAt, &c.SyncID)
	c.CreatedAt, c.UpdatedAt = c.CreatedAt.UTC(), c.UpdatedAt.UTC()
	return c, err
}

// ---- the cache ----------------------------------------------------------

// issue returns the issue from the cache, loading it from the database when
// the cache does not hold it.
func (s *server) issue(ctx context.Context, id string) (Issue, error) {
	s.mu.Lock()
	hit, ok := s.issues[id]
	s.mu.Unlock()
	if ok && time.Since(hit.loaded) < cacheTTL {
		return hit.value, nil
	}
	i, err := scanIssue(s.db.QueryRow(ctx, "SELECT "+issueColumns+" FROM issue WHERE id = $1", id))
	if err != nil {
		return Issue{}, err
	}
	s.putIssue(i)
	return i, nil
}

func (s *server) putIssue(i Issue) {
	s.mu.Lock()
	s.issues[i.ID] = cachedIssue{value: i, loaded: time.Now()}
	s.mu.Unlock()
}

func (s *server) dropIssue(id string, comments []string) {
	s.mu.Lock()
	delete(s.issues, id)
	for _, c := range comments {
		delete(s.comments, c)
	}
	s.mu.Unlock()
}

func (s *server) comment(ctx context.Context, id string) (Comment, error) {
	s.mu.Lock()
	hit, ok := s.comments[id]
	s.mu.Unlock()
	if ok && time.Since(hit.loaded) < cacheTTL {
		return hit.value, nil
	}
	c, err := scanComment(s.db.QueryRow(ctx, "SELECT "+commentColumns+" FROM comment WHERE id = $1", id))
	if err != nil {
		return Comment{}, err
	}
	s.putComment(c)
	return c, nil
}

func (s *server) putComment(c Comment) {
	s.mu.Lock()
	s.comments[c.ID] = cachedComment{value: c, loaded: time.Now()}
	s.mu.Unlock()
}

func (s *server) userWorkspace(ctx context.Context, id string) (int, bool) {
	s.mu.Lock()
	ws, ok := s.users[id]
	s.mu.Unlock()
	if ok {
		return ws, true
	}
	if err := s.db.QueryRow(ctx, "SELECT workspace_id FROM app_user WHERE id = $1", id).Scan(&ws); err != nil {
		return 0, false
	}
	s.mu.Lock()
	s.users[id] = ws
	s.mu.Unlock()
	return ws, true
}

func (s *server) teamWorkspace(ctx context.Context, id string) (int, bool) {
	s.mu.Lock()
	ws, ok := s.teams[id]
	s.mu.Unlock()
	if ok {
		return ws, true
	}
	if err := s.db.QueryRow(ctx, "SELECT workspace_id FROM team WHERE id = $1", id).Scan(&ws); err != nil {
		return 0, false
	}
	s.mu.Lock()
	s.teams[id] = ws
	s.mu.Unlock()
	return ws, true
}

func (s *server) lock(ws int) *sync.Mutex {
	m, _ := s.locks.LoadOrStore(ws, &sync.Mutex{})
	return m.(*sync.Mutex)
}

// ---- helpers ------------------------------------------------------------

func now() time.Time { return time.Now().UTC().Truncate(time.Microsecond) }

func newUUID() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		panic(err)
	}
	b[6] = (b[6] & 0x0f) | 0x40
	b[8] = (b[8] & 0x3f) | 0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:16])
}

func pgCode(err error) (code, constraint string) {
	var pgErr *pgconn.PgError
	if errors.As(err, &pgErr) {
		return pgErr.Code, pgErr.ConstraintName
	}
	return "", ""
}

func decode(w http.ResponseWriter, r *http.Request, into any) bool {
	body := http.MaxBytesReader(w, r.Body, 1<<20)
	decoder := json.NewDecoder(body)
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(into); err != nil {
		tracker.Fail(w, http.StatusBadRequest, "the request body is not what this route takes: %v", err)
		return false
	}
	return true
}

func broken(w http.ResponseWriter, err error) {
	log.Printf("database: %v", err)
	tracker.Fail(w, http.StatusInternalServerError, "tracker-api could not complete the request: %v", err)
}

// actor is the user a request acts as.
func (s *server) actor(w http.ResponseWriter, r *http.Request) (string, int, bool) {
	id := strings.TrimSpace(r.Header.Get("X-User-Id"))
	if id == "" {
		tracker.Fail(w, http.StatusUnauthorized, "X-User-Id is required: every request acts as a user")
		return "", 0, false
	}
	ws, ok := s.userWorkspace(r.Context(), id)
	if !ok {
		tracker.Fail(w, http.StatusForbidden, "unknown user %s", id)
		return "", 0, false
	}
	return id, ws, true
}

// record writes the sync action for a change, in the change's transaction.
func record(ctx context.Context, tx pgx.Tx, id int64, ws int, model, modelID, action string, entity, delta any, actor string, at time.Time) error {
	data, err := json.Marshal(entity)
	if err != nil {
		return err
	}
	var changed *string
	if delta != nil {
		raw, err := json.Marshal(delta)
		if err != nil {
			return err
		}
		text := string(raw)
		changed = &text
	}
	_, err = tx.Exec(ctx,
		`INSERT INTO sync_action (id, workspace_id, model, model_id, action, data, delta, actor_id, created_at)
		 VALUES ($1, $2, $3, $4, $5, $6::text::jsonb, $7::text::jsonb, $8, $9)`,
		id, ws, model, modelID, action, string(data), changed, actor, at)
	return err
}

func createdAt(given *time.Time, fallback time.Time) (time.Time, bool) {
	if given == nil {
		return fallback, true
	}
	at := given.UTC().Truncate(time.Microsecond)
	return at, !at.After(fallback.Add(5 * time.Second))
}

// ---- issues -------------------------------------------------------------

func (s *server) createIssue(w http.ResponseWriter, r *http.Request) {
	actor, actorWS, ok := s.actor(w, r)
	if !ok {
		return
	}
	var in struct {
		ID          *string    `json:"id"`
		TeamID      string     `json:"teamId"`
		Title       string     `json:"title"`
		Description *string    `json:"description"`
		State       *string    `json:"state"`
		Priority    *int       `json:"priority"`
		AssigneeID  *string    `json:"assigneeId"`
		CreatedAt   *time.Time `json:"createdAt"`
	}
	if !decode(w, r, &in) {
		return
	}
	ctx := r.Context()
	ws, ok := s.teamWorkspace(ctx, in.TeamID)
	if !ok {
		tracker.Fail(w, http.StatusBadRequest, "teamId is not a team")
		return
	}
	if ws != actorWS {
		tracker.Fail(w, http.StatusForbidden, "the team belongs to another workspace")
		return
	}
	at := now()
	issue := Issue{WorkspaceID: ws, TeamID: in.TeamID, Title: strings.TrimSpace(in.Title), State: "todo", CreatorID: actor, UpdatedAt: at}
	if issue.Title == "" {
		tracker.Fail(w, http.StatusBadRequest, "title is required")
		return
	}
	if in.ID != nil {
		if !uuidPattern.MatchString(*in.ID) {
			tracker.Fail(w, http.StatusBadRequest, "id must be a UUID")
			return
		}
		issue.ID = strings.ToLower(*in.ID)
	} else {
		issue.ID = newUUID()
	}
	if in.Description != nil {
		issue.Description = *in.Description
	}
	if in.State != nil {
		if !states[*in.State] {
			tracker.Fail(w, http.StatusBadRequest, "state must be one of backlog, todo, in_progress, done, canceled")
			return
		}
		issue.State = *in.State
	}
	if in.Priority != nil {
		if *in.Priority < 0 || *in.Priority > 4 {
			tracker.Fail(w, http.StatusBadRequest, "priority must be between 0 and 4")
			return
		}
		issue.Priority = *in.Priority
	}
	if in.AssigneeID != nil {
		if aws, ok := s.userWorkspace(ctx, *in.AssigneeID); !ok || aws != ws {
			tracker.Fail(w, http.StatusBadRequest, "assigneeId is not a user of this workspace")
			return
		}
		issue.AssigneeID = in.AssigneeID
	}
	if issue.CreatedAt, ok = createdAt(in.CreatedAt, at); !ok {
		tracker.Fail(w, http.StatusBadRequest, "createdAt must be in the past")
		return
	}

	lock := s.lock(ws)
	lock.Lock()
	defer lock.Unlock()
	tx, err := s.db.Begin(ctx)
	if err != nil {
		broken(w, err)
		return
	}
	defer tx.Rollback(ctx)
	if err := tx.QueryRow(ctx, "UPDATE team SET next_number = next_number + 1 WHERE id = $1 RETURNING next_number - 1", in.TeamID).Scan(&issue.Number); err != nil {
		broken(w, err)
		return
	}
	if err := tx.QueryRow(ctx, "SELECT nextval('sync_id_seq')").Scan(&issue.SyncID); err != nil {
		broken(w, err)
		return
	}
	_, err = tx.Exec(ctx,
		"INSERT INTO issue ("+issueColumns+") VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)",
		issue.ID, issue.WorkspaceID, issue.TeamID, issue.Number, issue.Title, issue.Description, issue.State, issue.Priority,
		issue.AssigneeID, issue.CreatorID, issue.CreatedAt, issue.UpdatedAt, issue.SyncID)
	if code, constraint := pgCode(err); code == "23505" && constraint == "issue_pkey" {
		tracker.Fail(w, http.StatusConflict, "an issue with id %s already exists", issue.ID)
		return
	}
	if err != nil {
		broken(w, err)
		return
	}
	if err := record(ctx, tx, issue.SyncID, ws, "issue", issue.ID, "I", issue, nil, actor, at); err != nil {
		broken(w, err)
		return
	}
	if err := tx.Commit(ctx); err != nil {
		broken(w, err)
		return
	}
	s.putIssue(issue)
	tracker.WriteJSON(w, http.StatusCreated, issue)
}

// readIssue loads an issue for a request, answering 404 or 403 itself.
func (s *server) readIssue(w http.ResponseWriter, r *http.Request, actorWS int) (Issue, bool) {
	issue, err := s.issue(r.Context(), r.PathValue("id"))
	if errors.Is(err, pgx.ErrNoRows) {
		tracker.Fail(w, http.StatusNotFound, "issue not found")
		return Issue{}, false
	}
	if err != nil {
		broken(w, err)
		return Issue{}, false
	}
	if issue.WorkspaceID != actorWS {
		tracker.Fail(w, http.StatusForbidden, "the issue belongs to another workspace")
		return Issue{}, false
	}
	return issue, true
}

func (s *server) getIssue(w http.ResponseWriter, r *http.Request) {
	_, actorWS, ok := s.actor(w, r)
	if !ok {
		return
	}
	if issue, ok := s.readIssue(w, r, actorWS); ok {
		tracker.WriteJSON(w, http.StatusOK, issue)
	}
}

func sameString(a, b *string) bool {
	if a == nil || b == nil {
		return a == nil && b == nil
	}
	return *a == *b
}

// change applies the fields of a PATCH to an issue. It returns the fields
// that changed, or a message when the request is not valid.
func (s *server) change(ctx context.Context, issue *Issue, in map[string]json.RawMessage) (map[string]any, string) {
	delta := map[string]any{}
	for name, raw := range in {
		switch name {
		case "title":
			var v string
			if json.Unmarshal(raw, &v) != nil || strings.TrimSpace(v) == "" {
				return nil, "title must be a non-empty string"
			}
			if v != issue.Title {
				issue.Title, delta[name] = v, v
			}
		case "description":
			var v string
			if json.Unmarshal(raw, &v) != nil {
				return nil, "description must be a string"
			}
			if v != issue.Description {
				issue.Description, delta[name] = v, v
			}
		case "state":
			var v string
			if json.Unmarshal(raw, &v) != nil || !states[v] {
				return nil, "state must be one of backlog, todo, in_progress, done, canceled"
			}
			if v != issue.State {
				issue.State, delta[name] = v, v
			}
		case "priority":
			var v int
			if json.Unmarshal(raw, &v) != nil || v < 0 || v > 4 {
				return nil, "priority must be between 0 and 4"
			}
			if v != issue.Priority {
				issue.Priority, delta[name] = v, v
			}
		case "assigneeId":
			var v *string
			if json.Unmarshal(raw, &v) != nil {
				return nil, "assigneeId must be a user id or null"
			}
			if v != nil {
				if ws, ok := s.userWorkspace(ctx, *v); !ok || ws != issue.WorkspaceID {
					return nil, "assigneeId is not a user of this workspace"
				}
			}
			if !sameString(v, issue.AssigneeID) {
				issue.AssigneeID, delta[name] = v, v
			}
		default:
			return nil, fmt.Sprintf("%q is not a field this route changes (title, description, state, priority, assigneeId)", name)
		}
	}
	return delta, ""
}

// saveIssue writes a changed issue and its sync action, provided the row is
// still the one the change was made from.
func (s *server) saveIssue(ctx context.Context, from int64, next *Issue, delta map[string]any, actor string) (bool, error) {
	tx, err := s.db.Begin(ctx)
	if err != nil {
		return false, err
	}
	defer tx.Rollback(ctx)
	if err := tx.QueryRow(ctx, "SELECT nextval('sync_id_seq')").Scan(&next.SyncID); err != nil {
		return false, err
	}
	tag, err := tx.Exec(ctx,
		`UPDATE issue SET title = $2, description = $3, state = $4, priority = $5, assignee_id = $6, updated_at = $7, sync_id = $8
		 WHERE id = $1 AND sync_id = $9`,
		next.ID, next.Title, next.Description, next.State, next.Priority, next.AssigneeID, next.UpdatedAt, next.SyncID, from)
	if err != nil {
		return false, err
	}
	if tag.RowsAffected() == 0 {
		return false, nil
	}
	if err := record(ctx, tx, next.SyncID, next.WorkspaceID, "issue", next.ID, "U", next, delta, actor, next.UpdatedAt); err != nil {
		return false, err
	}
	return true, tx.Commit(ctx)
}

func (s *server) updateIssue(w http.ResponseWriter, r *http.Request) {
	actor, actorWS, ok := s.actor(w, r)
	if !ok {
		return
	}
	var in map[string]json.RawMessage
	if !decode(w, r, &in) {
		return
	}
	first, ok := s.readIssue(w, r, actorWS)
	if !ok {
		return
	}
	lock := s.lock(first.WorkspaceID)
	lock.Lock()
	defer lock.Unlock()
	ctx := r.Context()
	for attempt := 0; attempt < 3; attempt++ {
		issue, ok := s.readIssue(w, r, actorWS)
		if !ok {
			return
		}
		if match := strings.Trim(r.Header.Get("If-Match"), `" `); match != "" {
			want, err := strconv.ParseInt(match, 10, 64)
			if err != nil {
				tracker.Fail(w, http.StatusBadRequest, "If-Match takes the issue's syncId")
				return
			}
			if want != issue.SyncID {
				tracker.WriteJSON(w, http.StatusPreconditionFailed, map[string]any{
					"error":  fmt.Sprintf("the issue has changed since syncId %d", want),
					"syncId": issue.SyncID,
				})
				return
			}
		}
		from := issue.SyncID
		delta, problem := s.change(ctx, &issue, in)
		if problem != "" {
			tracker.Fail(w, http.StatusBadRequest, "%s", problem)
			return
		}
		if len(delta) == 0 {
			tracker.WriteJSON(w, http.StatusOK, issue)
			return
		}
		issue.UpdatedAt = now()
		saved, err := s.saveIssue(ctx, from, &issue, delta, actor)
		if err != nil {
			broken(w, err)
			return
		}
		if saved {
			s.putIssue(issue)
			tracker.WriteJSON(w, http.StatusOK, issue)
			return
		}
		// The row is not the one the cache held: read it again.
		s.dropIssue(issue.ID, nil)
	}
	tracker.Fail(w, http.StatusConflict, "the issue kept changing; try again")
}

func (s *server) deleteIssue(w http.ResponseWriter, r *http.Request) {
	actor, actorWS, ok := s.actor(w, r)
	if !ok {
		return
	}
	first, ok := s.readIssue(w, r, actorWS)
	if !ok {
		return
	}
	lock := s.lock(first.WorkspaceID)
	lock.Lock()
	defer lock.Unlock()
	issue, ok := s.readIssue(w, r, actorWS)
	if !ok {
		return
	}
	ctx := r.Context()
	tx, err := s.db.Begin(ctx)
	if err != nil {
		broken(w, err)
		return
	}
	defer tx.Rollback(ctx)
	rows, err := tx.Query(ctx, "SELECT id FROM comment WHERE issue_id = $1", issue.ID)
	if err != nil {
		broken(w, err)
		return
	}
	comments, err := pgx.CollectRows(rows, pgx.RowTo[string])
	if err != nil {
		broken(w, err)
		return
	}
	tag, err := tx.Exec(ctx, "DELETE FROM issue WHERE id = $1", issue.ID)
	if err != nil {
		broken(w, err)
		return
	}
	if tag.RowsAffected() == 0 {
		s.dropIssue(issue.ID, comments)
		tracker.Fail(w, http.StatusNotFound, "issue not found")
		return
	}
	var syncID int64
	if err := tx.QueryRow(ctx, "SELECT nextval('sync_id_seq')").Scan(&syncID); err != nil {
		broken(w, err)
		return
	}
	if err := record(ctx, tx, syncID, issue.WorkspaceID, "issue", issue.ID, "D", issue, nil, actor, now()); err != nil {
		broken(w, err)
		return
	}
	if err := tx.Commit(ctx); err != nil {
		broken(w, err)
		return
	}
	s.dropIssue(issue.ID, comments)
	tracker.WriteJSON(w, http.StatusOK, map[string]any{"deleted": issue.ID, "syncId": syncID})
}

func (s *server) listIssues(w http.ResponseWriter, r *http.Request) {
	_, actorWS, ok := s.actor(w, r)
	if !ok {
		return
	}
	ws, err := strconv.Atoi(r.PathValue("ws"))
	if err != nil || ws != actorWS {
		tracker.Fail(w, http.StatusForbidden, "not your workspace")
		return
	}
	ctx := r.Context()
	rows, err := s.db.Query(ctx, "SELECT id FROM issue WHERE workspace_id = $1 ORDER BY team_id, number", ws)
	if err != nil {
		broken(w, err)
		return
	}
	ids, err := pgx.CollectRows(rows, pgx.RowTo[string])
	if err != nil {
		broken(w, err)
		return
	}
	issues := make([]Issue, 0, len(ids))
	for _, id := range ids {
		if issue, err := s.issue(ctx, id); err == nil {
			issues = append(issues, issue)
		}
	}
	tracker.WriteJSON(w, http.StatusOK, map[string]any{"issues": issues})
}

// ---- comments -----------------------------------------------------------

func (s *server) createComment(w http.ResponseWriter, r *http.Request) {
	actor, actorWS, ok := s.actor(w, r)
	if !ok {
		return
	}
	var in struct {
		ID        *string    `json:"id"`
		Body      string     `json:"body"`
		CreatedAt *time.Time `json:"createdAt"`
	}
	if !decode(w, r, &in) {
		return
	}
	issue, ok := s.readIssue(w, r, actorWS)
	if !ok {
		return
	}
	at := now()
	comment := Comment{WorkspaceID: issue.WorkspaceID, IssueID: issue.ID, UserID: actor, Body: in.Body, UpdatedAt: at}
	if strings.TrimSpace(comment.Body) == "" {
		tracker.Fail(w, http.StatusBadRequest, "body is required")
		return
	}
	if in.ID != nil {
		if !uuidPattern.MatchString(*in.ID) {
			tracker.Fail(w, http.StatusBadRequest, "id must be a UUID")
			return
		}
		comment.ID = strings.ToLower(*in.ID)
	} else {
		comment.ID = newUUID()
	}
	if comment.CreatedAt, ok = createdAt(in.CreatedAt, at); !ok {
		tracker.Fail(w, http.StatusBadRequest, "createdAt must be in the past")
		return
	}

	lock := s.lock(issue.WorkspaceID)
	lock.Lock()
	defer lock.Unlock()
	ctx := r.Context()
	tx, err := s.db.Begin(ctx)
	if err != nil {
		broken(w, err)
		return
	}
	defer tx.Rollback(ctx)
	if err := tx.QueryRow(ctx, "SELECT nextval('sync_id_seq')").Scan(&comment.SyncID); err != nil {
		broken(w, err)
		return
	}
	_, err = tx.Exec(ctx,
		"INSERT INTO comment ("+commentColumns+") VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
		comment.ID, comment.WorkspaceID, comment.IssueID, comment.UserID, comment.Body, comment.CreatedAt, comment.UpdatedAt, comment.SyncID)
	switch code, _ := pgCode(err); {
	case code == "23505":
		tracker.Fail(w, http.StatusConflict, "a comment with id %s already exists", comment.ID)
		return
	case code == "23503":
		// The issue the cache held is no longer in the database.
		s.dropIssue(issue.ID, nil)
		tracker.Fail(w, http.StatusNotFound, "issue not found")
		return
	case err != nil:
		broken(w, err)
		return
	}
	if err := record(ctx, tx, comment.SyncID, comment.WorkspaceID, "comment", comment.ID, "I", comment, nil, actor, at); err != nil {
		broken(w, err)
		return
	}
	if err := tx.Commit(ctx); err != nil {
		broken(w, err)
		return
	}
	s.putComment(comment)
	tracker.WriteJSON(w, http.StatusCreated, comment)
}

func (s *server) listComments(w http.ResponseWriter, r *http.Request) {
	_, actorWS, ok := s.actor(w, r)
	if !ok {
		return
	}
	issue, ok := s.readIssue(w, r, actorWS)
	if !ok {
		return
	}
	ctx := r.Context()
	rows, err := s.db.Query(ctx, "SELECT id FROM comment WHERE issue_id = $1 ORDER BY created_at, id", issue.ID)
	if err != nil {
		broken(w, err)
		return
	}
	ids, err := pgx.CollectRows(rows, pgx.RowTo[string])
	if err != nil {
		broken(w, err)
		return
	}
	comments := make([]Comment, 0, len(ids))
	for _, id := range ids {
		if comment, err := s.comment(ctx, id); err == nil {
			comments = append(comments, comment)
		}
	}
	tracker.WriteJSON(w, http.StatusOK, map[string]any{"comments": comments})
}

// ---- the sync engine ----------------------------------------------------

func workspaceParam(w http.ResponseWriter, r *http.Request) (int, bool) {
	ws, err := strconv.Atoi(r.URL.Query().Get("workspace"))
	if err != nil {
		tracker.Fail(w, http.StatusBadRequest, "workspace is required")
		return 0, false
	}
	return ws, true
}

// bootstrap is what a user's app downloads first: its whole workspace, and
// the sync id that copy is current to.
func (s *server) bootstrap(w http.ResponseWriter, r *http.Request) {
	ws, ok := workspaceParam(w, r)
	if !ok {
		return
	}
	ctx := r.Context()
	tx, err := s.db.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.RepeatableRead, AccessMode: pgx.ReadOnly})
	if err != nil {
		broken(w, err)
		return
	}
	defer tx.Rollback(ctx)
	var last int64
	if err := tx.QueryRow(ctx, "SELECT coalesce(max(id), 0) FROM sync_action WHERE workspace_id = $1", ws).Scan(&last); err != nil {
		broken(w, err)
		return
	}
	type person struct {
		ID    string `json:"id"`
		Name  string `json:"name"`
		Email string `json:"email"`
	}
	type team struct {
		ID   string `json:"id"`
		Key  string `json:"key"`
		Name string `json:"name"`
	}
	rows, err := tx.Query(ctx, "SELECT id, name, email FROM app_user WHERE workspace_id = $1 ORDER BY name", ws)
	if err != nil {
		broken(w, err)
		return
	}
	users, err := pgx.CollectRows(rows, func(row pgx.CollectableRow) (person, error) {
		var p person
		err := row.Scan(&p.ID, &p.Name, &p.Email)
		return p, err
	})
	if err != nil {
		broken(w, err)
		return
	}
	rows, err = tx.Query(ctx, "SELECT id, key, name FROM team WHERE workspace_id = $1 ORDER BY key", ws)
	if err != nil {
		broken(w, err)
		return
	}
	teams, err := pgx.CollectRows(rows, func(row pgx.CollectableRow) (team, error) {
		var t team
		err := row.Scan(&t.ID, &t.Key, &t.Name)
		return t, err
	})
	if err != nil {
		broken(w, err)
		return
	}
	rows, err = tx.Query(ctx, "SELECT "+issueColumns+" FROM issue WHERE workspace_id = $1 ORDER BY team_id, number", ws)
	if err != nil {
		broken(w, err)
		return
	}
	issues, err := pgx.CollectRows(rows, func(row pgx.CollectableRow) (Issue, error) { return scanIssue(row) })
	if err != nil {
		broken(w, err)
		return
	}
	rows, err = tx.Query(ctx, "SELECT "+commentColumns+" FROM comment WHERE workspace_id = $1 ORDER BY created_at, id", ws)
	if err != nil {
		broken(w, err)
		return
	}
	comments, err := pgx.CollectRows(rows, func(row pgx.CollectableRow) (Comment, error) { return scanComment(row) })
	if err != nil {
		broken(w, err)
		return
	}
	tracker.WriteJSON(w, http.StatusOK, map[string]any{
		"workspaceId": ws, "lastSyncId": last, "users": users, "teams": teams, "issues": issues, "comments": comments,
	})
}

// delta is how a user's app stays current: the sync actions after the last
// one it has.
func (s *server) delta(w http.ResponseWriter, r *http.Request) {
	ws, ok := workspaceParam(w, r)
	if !ok {
		return
	}
	after, _ := strconv.ParseInt(r.URL.Query().Get("after"), 10, 64)
	limit, err := strconv.Atoi(r.URL.Query().Get("limit"))
	if err != nil || limit < 1 || limit > 2000 {
		limit = 500
	}
	rows, err := s.db.Query(r.Context(),
		`SELECT id, workspace_id, model, model_id, action, data::text, delta::text, coalesce(actor_id, ''), created_at
		 FROM sync_action WHERE workspace_id = $1 AND id > $2 ORDER BY id LIMIT $3`, ws, after, limit)
	if err != nil {
		broken(w, err)
		return
	}
	actions, err := pgx.CollectRows(rows, func(row pgx.CollectableRow) (Action, error) {
		var a Action
		var data string
		var delta *string
		if err := row.Scan(&a.ID, &a.WorkspaceID, &a.Model, &a.ModelID, &a.Action, &data, &delta, &a.ActorID, &a.CreatedAt); err != nil {
			return a, err
		}
		a.Data = json.RawMessage(data)
		if delta != nil {
			a.Delta = json.RawMessage(*delta)
		}
		a.CreatedAt = a.CreatedAt.UTC()
		return a, nil
	})
	if err != nil {
		broken(w, err)
		return
	}
	last := after
	if len(actions) > 0 {
		last = actions[len(actions)-1].ID
	}
	tracker.WriteJSON(w, http.StatusOK, map[string]any{"actions": actions, "lastSyncId": last})
}

// ---- administration -----------------------------------------------------

func (s *server) getMaintenance(w http.ResponseWriter, _ *http.Request) {
	tracker.WriteJSON(w, http.StatusOK, map[string]bool{"enabled": s.maintenance.Load()})
}

func (s *server) setMaintenance(w http.ResponseWriter, r *http.Request) {
	var in struct {
		Enabled bool `json:"enabled"`
	}
	if !decode(w, r, &in) {
		return
	}
	s.maintenance.Store(in.Enabled)
	log.Printf("maintenance mode: %v", in.Enabled)
	tracker.WriteJSON(w, http.StatusOK, map[string]bool{"enabled": in.Enabled})
}

func (s *server) addRecoveryItem(w http.ResponseWriter, r *http.Request) {
	var in struct {
		Workspace int    `json:"workspace"`
		Action    int64  `json:"action"`
		Note      string `json:"note"`
	}
	if !decode(w, r, &in) {
		return
	}
	if in.Action <= 0 || strings.TrimSpace(in.Note) == "" {
		tracker.Fail(w, http.StatusBadRequest, "action (the lost sync action's id) and note are required")
		return
	}
	_, err := s.db.Exec(r.Context(),
		`INSERT INTO recovery_item (workspace_id, action_id, note) VALUES ($1, $2, $3)
		 ON CONFLICT (workspace_id, action_id) DO UPDATE SET note = EXCLUDED.note`, in.Workspace, in.Action, in.Note)
	if code, _ := pgCode(err); code == "23503" {
		tracker.Fail(w, http.StatusBadRequest, "workspace is not a workspace")
		return
	}
	if err != nil {
		broken(w, err)
		return
	}
	tracker.WriteJSON(w, http.StatusCreated, in)
}

func (s *server) recoveryPage(w http.ResponseWriter, r *http.Request) {
	ws, err := strconv.Atoi(r.PathValue("ws"))
	if err != nil {
		tracker.Fail(w, http.StatusBadRequest, "workspace must be a number")
		return
	}
	type item struct {
		Action    int64     `json:"action"`
		Note      string    `json:"note"`
		CreatedAt time.Time `json:"createdAt"`
	}
	rows, err := s.db.Query(r.Context(), "SELECT action_id, note, created_at FROM recovery_item WHERE workspace_id = $1 ORDER BY action_id", ws)
	if err != nil {
		broken(w, err)
		return
	}
	items, err := pgx.CollectRows(rows, func(row pgx.CollectableRow) (item, error) {
		var i item
		err := row.Scan(&i.Action, &i.Note, &i.CreatedAt)
		return i, err
	})
	if err != nil {
		broken(w, err)
		return
	}
	tracker.WriteJSON(w, http.StatusOK, map[string]any{"workspace": ws, "items": items})
}

func (s *server) healthz(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
	defer cancel()
	if err := s.db.Ping(ctx); err != nil {
		tracker.Fail(w, http.StatusServiceUnavailable, "database: %v", err)
		return
	}
	tracker.WriteJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (s *server) index(w http.ResponseWriter, _ *http.Request) {
	tracker.WriteJSON(w, http.StatusOK, map[string]any{
		"service": "tracker-api",
		"about": []string{
			"Every request acts as a user: send that user's id as X-User-Id. /sync and /admin routes take none.",
			"Entities are read through a cache in front of the database. An entity stays in the cache for 24 hours.",
			"Every change is written with a sync action (I insert, U update, D delete): the entity after the change (data), the fields it changed (delta) and the user who made it (actorId). Users' apps hold a copy of their workspace and follow these actions.",
			"Clients generate ids: id is optional on create. createdAt is optional on create, for imports, and must be in the past.",
			"A PATCH sent with If-Match: <syncId> is applied only if the issue's syncId is still that value; otherwise it answers 412 and changes nothing.",
		},
		"routes": map[string]string{
			"GET /workspaces/{workspace}/issues":   "the workspace's issues",
			"POST /issues":                         "create an issue: teamId, title, [description, state, priority, assigneeId, id, createdAt]; the team's next number is assigned",
			"GET /issues/{id}":                     "an issue",
			"PATCH /issues/{id}":                   "change any of title, description, state, priority, assigneeId",
			"DELETE /issues/{id}":                  "delete an issue and its comments",
			"GET /issues/{id}/comments":            "an issue's comments",
			"POST /issues/{id}/comments":           "comment on an issue: body, [id, createdAt]",
			"GET /sync/bootstrap?workspace=":       "a workspace in full, and the sync id it is current to",
			"GET /sync/delta?workspace=&after=":    "the workspace's sync actions after a sync id",
			"GET /workspaces/{workspace}/recovery": "the workspace's recovery page: lost changes that could not be put back, as its admins see them",
			"POST /admin/recovery-items":           "add to a recovery page: workspace, action (the lost sync action's id), note",
			"GET /admin/maintenance":               "whether Tracker is in maintenance mode",
			"PUT /admin/maintenance":               "{\"enabled\": true} takes Tracker offline for every user; false brings it back",
		},
	})
}

// gate answers every user with the maintenance screen while Tracker is in
// maintenance mode.
func (s *server) gate(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		open := r.URL.Path == "/" || r.URL.Path == "/healthz" || strings.HasPrefix(r.URL.Path, "/admin/")
		if s.maintenance.Load() && !open {
			tracker.Fail(w, http.StatusServiceUnavailable, "Tracker is down for maintenance")
			return
		}
		next.ServeHTTP(w, r)
	})
}

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, os.Interrupt)
	defer stop()
	s := &server{
		db:       tracker.Connect(ctx, "DATABASE_URL", 16),
		issues:   map[string]cachedIssue{},
		comments: map[string]cachedComment{},
		users:    map[string]int{},
		teams:    map[string]int{},
	}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /{$}", s.index)
	mux.HandleFunc("GET /healthz", s.healthz)
	mux.HandleFunc("GET /workspaces/{ws}/issues", s.listIssues)
	mux.HandleFunc("GET /workspaces/{ws}/recovery", s.recoveryPage)
	mux.HandleFunc("POST /issues", s.createIssue)
	mux.HandleFunc("GET /issues/{id}", s.getIssue)
	mux.HandleFunc("PATCH /issues/{id}", s.updateIssue)
	mux.HandleFunc("DELETE /issues/{id}", s.deleteIssue)
	mux.HandleFunc("GET /issues/{id}/comments", s.listComments)
	mux.HandleFunc("POST /issues/{id}/comments", s.createComment)
	mux.HandleFunc("GET /sync/bootstrap", s.bootstrap)
	mux.HandleFunc("GET /sync/delta", s.delta)
	mux.HandleFunc("GET /admin/maintenance", s.getMaintenance)
	mux.HandleFunc("PUT /admin/maintenance", s.setMaintenance)
	mux.HandleFunc("POST /admin/recovery-items", s.addRecoveryItem)

	srv := &http.Server{Addr: ":8080", Handler: s.gate(mux), ReadHeaderTimeout: 5 * time.Second}
	go func() {
		<-ctx.Done()
		// Finish the requests in hand before stopping.
		shutdown, cancel := context.WithTimeout(context.Background(), 20*time.Second)
		defer cancel()
		_ = srv.Shutdown(shutdown)
	}()
	log.Printf("tracker-api listening on %s", srv.Addr)
	if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatal(err)
	}
}
