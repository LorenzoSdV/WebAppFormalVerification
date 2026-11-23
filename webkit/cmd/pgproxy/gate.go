package main

// The gate: holding and releasing statements, for deterministic reproduction.
//
// While a set of trace ids is gated, every statement (simple Query or Parse) whose
// sqlcommenter traceparent carries one of those ids is held before it reaches the
// server, until the controller releases it. Holding a statement only delays it;
// its bytes are forwarded unchanged afterwards.
//
// Control API (HTTP, WK_CONTROL, default ":6433"):
//
//	POST   /gate     {"trace_ids": [...]}   gate these requests (replaces the set)
//	DELETE /gate                            stop gating and release everything held
//	GET    /held                            held statements
//	POST   /release  {"id": N}              let held statement N through
//	GET    /conns                           sessions: backend pid, busy, waiting on a lock,
//	                                        statements sent / finished
//
// "busy" means the server is still working on something this connection sent;
// "lock_wait" comes from pg_stat_activity (wait_event_type = 'Lock') and needs
// WK_DB_URL, a direct connection to the database.

import (
	"context"
	"encoding/json"
	"log"
	"net/http"
	"sort"
	"sync"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgproto3"

	"github.com/LorenzoSdV/WebAppFormalVerification/webkit/internal/ndjson"
)

type hold struct {
	ID      int64  `json:"id"`
	Conn    int64  `json:"conn"`
	PID     uint32 `json:"pid"`
	TraceID string `json:"trace_id"`
	SQL     string `json:"sql"`
	SinceNs int64  `json:"since_ns"`
	release chan struct{}
}

type gateState struct {
	mu       sync.Mutex
	traceIDs map[string]bool
	holds    map[int64]*hold
	nextID   int64
	sessions map[int64]*session
}

var gate = &gateState{traceIDs: map[string]bool{}, holds: map[int64]*hold{}, sessions: map[int64]*session{}}

func (g *gateState) register(s *session)   { g.mu.Lock(); g.sessions[s.id] = s; g.mu.Unlock() }
func (g *gateState) unregister(s *session) { g.mu.Lock(); delete(g.sessions, s.id); g.mu.Unlock() }

// wait blocks the calling session's client-to-server pump while its statement is held.
func (g *gateState) wait(s *session, sql string, w *ndjson.Writer) {
	m := traceparentRe.FindStringSubmatch(sql)
	if m == nil {
		return
	}
	g.mu.Lock()
	if !g.traceIDs[m[2]] {
		g.mu.Unlock()
		return
	}
	g.nextID++
	s.mu.Lock()
	pid := s.pid
	s.mu.Unlock()
	h := &hold{ID: g.nextID, Conn: s.id, PID: pid, TraceID: m[2],
		SQL: commentRe.ReplaceAllString(sql, ""), SinceNs: ndjson.Now(), release: make(chan struct{})}
	g.holds[h.ID] = h
	g.mu.Unlock()
	w.Emit("hold", map[string]any{"hold": h.ID, "conn": h.Conn, "trace_id": h.TraceID, "sql_norm": h.SQL})
	<-h.release
	w.Emit("release", map[string]any{"hold": h.ID, "conn": h.Conn, "trace_id": h.TraceID})
}

func (g *gateState) releaseOne(id int64) bool {
	g.mu.Lock()
	defer g.mu.Unlock()
	h, ok := g.holds[id]
	if ok {
		delete(g.holds, id)
		close(h.release)
	}
	return ok
}

func (g *gateState) clear() {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.traceIDs = map[string]bool{}
	for id, h := range g.holds {
		delete(g.holds, id)
		close(h.release)
	}
}

func queryText(typ byte, body []byte) string {
	switch typ {
	case 'Q':
		var m pgproto3.Query
		if m.Decode(body) == nil {
			return m.String
		}
	case 'P':
		var m pgproto3.Parse
		if m.Decode(body) == nil {
			return m.Query
		}
	}
	return ""
}

func writeJSON(rw http.ResponseWriter, v any) {
	rw.Header().Set("content-type", "application/json")
	_ = json.NewEncoder(rw).Encode(v)
}

// lockWaiters returns, for each given backend that is waiting on a lock, the backends blocking it.
func lockWaiters(ctx context.Context, dbURL string, pids []uint32) map[uint32][]uint32 {
	out := map[uint32][]uint32{}
	if dbURL == "" || len(pids) == 0 {
		return out
	}
	cctx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	conn, err := pgx.Connect(cctx, dbURL)
	if err != nil {
		log.Printf("lock-wait query: %v", err)
		return out
	}
	defer conn.Close(context.Background())
	ints := make([]int32, len(pids))
	for i, p := range pids {
		ints[i] = int32(p)
	}
	rows, err := conn.Query(cctx, "SELECT pid, pg_blocking_pids(pid) FROM pg_stat_activity "+
		"WHERE pid = ANY($1) AND wait_event_type = 'Lock'", ints)
	if err != nil {
		return out
	}
	defer rows.Close()
	for rows.Next() {
		var pid int32
		var blockers []int32
		if rows.Scan(&pid, &blockers) == nil {
			list := []uint32{}
			for _, b := range blockers {
				list = append(list, uint32(b))
			}
			out[uint32(pid)] = list
		}
	}
	return out
}

func serveControl(addr, dbURL string, w *ndjson.Writer) {
	mux := http.NewServeMux()
	mux.HandleFunc("/gate", func(rw http.ResponseWriter, r *http.Request) {
		switch r.Method {
		case http.MethodPost:
			var req struct {
				TraceIDs []string `json:"trace_ids"`
			}
			if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
				http.Error(rw, err.Error(), http.StatusBadRequest)
				return
			}
			gate.mu.Lock()
			gate.traceIDs = map[string]bool{}
			for _, t := range req.TraceIDs {
				gate.traceIDs[t] = true
			}
			gate.mu.Unlock()
			w.Emit("gate", map[string]any{"trace_ids": req.TraceIDs})
			writeJSON(rw, map[string]any{"gated": len(req.TraceIDs)})
		case http.MethodDelete:
			gate.clear()
			w.Emit("gate_cleared", map[string]any{})
			writeJSON(rw, map[string]any{"cleared": true})
		default:
			http.Error(rw, "method not allowed", http.StatusMethodNotAllowed)
		}
	})
	mux.HandleFunc("/held", func(rw http.ResponseWriter, _ *http.Request) {
		gate.mu.Lock()
		out := make([]*hold, 0, len(gate.holds))
		for _, h := range gate.holds {
			out = append(out, h)
		}
		gate.mu.Unlock()
		sort.Slice(out, func(i, j int) bool { return out[i].ID < out[j].ID })
		writeJSON(rw, out)
	})
	mux.HandleFunc("/release", func(rw http.ResponseWriter, r *http.Request) {
		var req struct {
			ID int64 `json:"id"`
		}
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			http.Error(rw, err.Error(), http.StatusBadRequest)
			return
		}
		writeJSON(rw, map[string]any{"released": gate.releaseOne(req.ID)})
	})
	mux.HandleFunc("/conns", func(rw http.ResponseWriter, r *http.Request) {
		type connInfo struct {
			Conn     int64  `json:"conn"`
			PID      uint32 `json:"pid"`
			Busy     bool   `json:"busy"`
			LockWait bool     `json:"lock_wait"`
			Blockers []uint32 `json:"blocked_by"`
			Sent     int64    `json:"sent"`
			Finished int64  `json:"finished"`
		}
		gate.mu.Lock()
		var infos []connInfo
		var pids []uint32
		for _, s := range gate.sessions {
			s.mu.Lock()
			busy := len(s.queue) > 0
			infos = append(infos, connInfo{Conn: s.id, PID: s.pid, Busy: busy, Sent: s.stmtSeq, Finished: s.finished})
			if busy {
				pids = append(pids, s.pid)
			}
			s.mu.Unlock()
		}
		gate.mu.Unlock()
		waiting := lockWaiters(r.Context(), dbURL, pids)
		for i := range infos {
			blockers, ok := waiting[infos[i].PID]
			infos[i].LockWait = ok
			infos[i].Blockers = blockers
		}
		sort.Slice(infos, func(i, j int) bool { return infos[i].Conn < infos[j].Conn })
		writeJSON(rw, infos)
	})
	log.Printf("wk-pgproxy control API on %s", addr)
	log.Fatal(http.ListenAndServe(addr, mux))
}
