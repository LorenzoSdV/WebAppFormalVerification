// wk-pgproxy is a transparent PostgreSQL wire-protocol proxy.
//
// It forwards every byte unchanged and records, per statement:
//   - the SQL text, bind parameters, and the sqlcommenter traceparent
//     (which ties the statement to the HTTP request that issued it);
//   - send/complete timestamps, result columns and rows (capped), the command
//     tag, and any error with its SQLSTATE;
//   - the connection and transaction it ran in.
//
// Just before forwarding a COMMIT of an open transaction it asks the server for
// the transaction's xid (pg_current_xact_id_if_assigned: read-only, assigns
// nothing), so proxy transactions can be joined to logical-decoding records.
//
// Configuration (environment):
//
//	WK_LISTEN    listen address            (default ":6432")
//	WK_UPSTREAM  PostgreSQL address        (default "db:5432")
//	WK_OUT       output NDJSON file        (default "/wk/out/pgproxy.ndjson")
//	WK_MAX_ROWS  result rows kept per stmt (default 50)
//	WK_XID       "0" disables xid capture  (default on)
//	WK_CONTROL   control API address       (default ":6433"; see gate.go)
//	WK_DB_URL    direct DB URL, for lock-wait detection in the control API
package main

import (
	"bufio"
	"encoding/hex"
	"fmt"
	"log"
	"net"
	"os"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"

	"github.com/jackc/pgx/v5/pgproto3"

	"github.com/LorenzoSdV/WebAppFormalVerification/webkit/internal/ndjson"
	"github.com/LorenzoSdV/WebAppFormalVerification/webkit/internal/pgwire"
)

const xidQuery = "SELECT pg_current_xact_id_if_assigned()::text"

var (
	traceparentRe = regexp.MustCompile(`traceparent='([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})'`)
	commentRe     = regexp.MustCompile(`(?s)/\*.*?\*/`)
	commitRe      = regexp.MustCompile(`(?i)^\s*(commit|end)\b`)
	beginRe       = regexp.MustCompile(`(?i)^\s*(begin|start\s+transaction)\b`)
)

type config struct {
	listen, upstream, out string
	maxRows               int
	captureXid            bool
}

func env(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func main() {
	maxRows, err := strconv.Atoi(env("WK_MAX_ROWS", "50"))
	if err != nil {
		log.Fatalf("WK_MAX_ROWS: %v", err)
	}
	cfg := config{
		listen:     env("WK_LISTEN", ":6432"),
		upstream:   env("WK_UPSTREAM", "db:5432"),
		out:        env("WK_OUT", "/wk/out/pgproxy.ndjson"),
		maxRows:    maxRows,
		captureXid: env("WK_XID", "1") != "0",
	}
	w, err := ndjson.Open(cfg.out, "pgproxy")
	if err != nil {
		log.Fatal(err)
	}
	ln, err := net.Listen("tcp", cfg.listen)
	if err != nil {
		log.Fatal(err)
	}
	w.Emit("ready", map[string]any{"listen": cfg.listen, "upstream": cfg.upstream})
	log.Printf("wk-pgproxy listening on %s -> %s", cfg.listen, cfg.upstream)
	go serveControl(env("WK_CONTROL", ":6433"), os.Getenv("WK_DB_URL"), w)

	var connSeq atomic.Int64
	for {
		c, err := ln.Accept()
		if err != nil {
			log.Printf("accept: %v", err)
			continue
		}
		go handle(c, connSeq.Add(1), cfg, w)
	}
}

// handle runs the startup phase, then pumps both directions.
func handle(client net.Conn, id int64, cfg config, w *ndjson.Writer) {
	defer client.Close()
	cr := bufio.NewReader(client)

	var startup []byte
	for startup == nil {
		raw, code, err := pgwire.ReadStartup(cr)
		if err != nil {
			return
		}
		switch code {
		case pgwire.SSLRequestCode, pgwire.GSSEncRequestCode:
			// Tell the client to continue unencrypted so the stream stays readable.
			if _, err := client.Write([]byte{'N'}); err != nil {
				return
			}
		case pgwire.CancelRequestCode:
			w.Emit("cancel_request", map[string]any{"conn": id})
			if up, err := net.Dial("tcp", cfg.upstream); err == nil {
				_, _ = up.Write(raw)
				_ = up.Close()
			}
			return
		default:
			startup = raw
		}
	}

	server, err := net.Dial("tcp", cfg.upstream)
	if err != nil {
		w.Emit("upstream_error", map[string]any{"conn": id, "error": err.Error()})
		return
	}
	defer server.Close()
	if _, err := server.Write(startup); err != nil {
		return
	}

	params := pgwire.StartupParams(startup)
	w.Emit("conn_open", map[string]any{
		"conn": id, "client": client.RemoteAddr().String(),
		"user": params["user"], "database": params["database"], "application_name": params["application_name"],
	})

	s := &session{
		id: id, cfg: cfg, w: w, client: client, server: server,
		txStatus: 'I', prepared: map[string]string{}, portals: map[string]portal{},
	}
	gate.register(s)
	defer gate.unregister(s)
	done := make(chan struct{})
	go func() {
		s.serverToClient(bufio.NewReader(server))
		_ = client.Close()
		close(done)
	}()
	s.clientToServer(cr)
	_ = server.Close()
	<-done
	w.Emit("conn_close", map[string]any{"conn": id, "statements": s.stmtSeq})
}

type portal struct {
	sql    string
	params []any
}

// item is one client message still waiting for its server response(s).
type item struct {
	kind           byte // 'P','B','D','E','C','S','Q', or 'X' (injected xid query)
	stmt           *stmtRec
	describePortal bool
}

type stmtRec struct {
	conn       int64
	seq        int64
	protocol   string
	sql        string
	sqlNorm    string
	params     []any
	traceID    string
	spanID     string
	txn        string
	tSendNs    int64
	tDoneNs    int64
	columns    []string
	formats    []int16
	rows       [][]any
	rowsSeen   int
	truncated  bool
	tag        string
	errCode    string
	errMessage string
}

type session struct {
	id             int64
	cfg            config
	w              *ndjson.Writer
	client, server net.Conn

	mu          sync.Mutex
	pid         uint32 // server backend process id (BackendKeyData)
	queue       []*item
	txStatus    byte // from the last ReadyForQuery: 'I' idle, 'T' in txn, 'E' failed txn
	txnSeq      int64
	curTxn      string
	txnTraceID  string
	lastTag     string
	pendingXid  string
	stmtSeq     int64 // statements sent to the server
	finished    int64 // statements whose result came back
	prepared    map[string]string
	portals     map[string]portal
	nextRowDesc *pgproto3.RowDescription // from Describe(portal), for the following Execute
}

func (s *session) newStmt(protocol, sql string, params []any) *stmtRec {
	s.stmtSeq++
	rec := &stmtRec{
		conn: s.id, seq: s.stmtSeq, protocol: protocol, sql: sql,
		sqlNorm: strings.TrimSpace(commentRe.ReplaceAllString(sql, "")),
		params:  params, tSendNs: ndjson.Now(),
	}
	if m := traceparentRe.FindStringSubmatch(sql); m != nil {
		rec.traceID, rec.spanID = m[2], m[3]
	}
	isBegin := beginRe.MatchString(rec.sqlNorm)
	if isBegin {
		s.txnSeq++
		s.curTxn = fmt.Sprintf("c%d.t%d", s.id, s.txnSeq)
		s.txnTraceID = rec.traceID
		s.pendingXid = ""
	}
	if s.curTxn != "" && (s.txStatus != 'I' || isBegin) {
		rec.txn = s.curTxn
	}
	return rec
}

func (s *session) clientToServer(r *bufio.Reader) {
	for {
		typ, body, raw, err := pgwire.ReadMessage(r)
		if err != nil {
			return
		}
		if typ == 'Q' || typ == 'P' {
			gate.wait(s, queryText(typ, body), s.w) // blocks while the statement is held
		}
		inject := false
		s.mu.Lock()
		switch typ {
		case 'Q':
			var m pgproto3.Query
			if m.Decode(body) == nil {
				rec := s.newStmt("simple", m.String, nil)
				if s.cfg.captureXid && s.txStatus == 'T' && len(s.queue) == 0 && commitRe.MatchString(rec.sqlNorm) {
					inject = true
					s.queue = append(s.queue, &item{kind: 'X'})
				}
				s.queue = append(s.queue, &item{kind: 'Q', stmt: rec})
			}
		case 'P':
			var m pgproto3.Parse
			if m.Decode(body) == nil {
				s.prepared[m.Name] = m.Query
			}
			s.queue = append(s.queue, &item{kind: 'P'})
		case 'B':
			var m pgproto3.Bind
			if m.Decode(body) == nil {
				s.portals[m.DestinationPortal] = portal{sql: s.prepared[m.PreparedStatement], params: decodeParams(&m)}
			}
			s.queue = append(s.queue, &item{kind: 'B'})
		case 'D':
			var m pgproto3.Describe
			describePortal := m.Decode(body) == nil && m.ObjectType == 'P'
			s.queue = append(s.queue, &item{kind: 'D', describePortal: describePortal})
		case 'E':
			var m pgproto3.Execute
			name := ""
			if m.Decode(body) == nil {
				name = m.Portal
			}
			p := s.portals[name]
			rec := s.newStmt("extended", p.sql, p.params)
			if s.nextRowDesc != nil {
				rec.columns, rec.formats = columns(s.nextRowDesc)
				s.nextRowDesc = nil
			}
			s.queue = append(s.queue, &item{kind: 'E', stmt: rec})
		case 'C':
			s.queue = append(s.queue, &item{kind: 'C'})
		case 'S':
			s.queue = append(s.queue, &item{kind: 'S'})
		}
		s.mu.Unlock()

		if inject {
			if _, err := s.server.Write(pgwire.QueryFrame(xidQuery)); err != nil {
				return
			}
		}
		if _, err := s.server.Write(raw); err != nil {
			return
		}
	}
}

func (s *session) serverToClient(r *bufio.Reader) {
	for {
		typ, body, raw, err := pgwire.ReadMessage(r)
		if err != nil {
			return
		}
		s.mu.Lock()
		forward := s.onServerMessage(typ, body)
		s.mu.Unlock()
		if forward {
			if _, err := s.client.Write(raw); err != nil {
				return
			}
		}
	}
}

func (s *session) head() *item {
	if len(s.queue) == 0 {
		return nil
	}
	return s.queue[0]
}

func (s *session) pop() { s.queue = s.queue[1:] }

func (s *session) popKind(kind byte) {
	if h := s.head(); h != nil && h.kind == kind {
		s.pop()
	}
}

// onServerMessage updates state for one server message. It returns false for
// responses to the proxy's own injected query, which the client never sees.
func (s *session) onServerMessage(typ byte, body []byte) bool {
	h := s.head()
	if h != nil && h.kind == 'X' {
		switch typ {
		case 'D':
			var m pgproto3.DataRow
			if m.Decode(body) == nil && len(m.Values) == 1 && m.Values[0] != nil {
				s.pendingXid = string(m.Values[0])
			}
		case 'Z':
			s.pop()
		}
		return false
	}

	switch typ {
	case 'K': // BackendKeyData
		var m pgproto3.BackendKeyData
		if m.Decode(body) == nil {
			s.pid = m.ProcessID
		}
	case '1': // ParseComplete
		s.popKind('P')
	case '2': // BindComplete
		s.popKind('B')
	case '3': // CloseComplete
		s.popKind('C')
	case 'T': // RowDescription
		var m pgproto3.RowDescription
		if m.Decode(body) != nil || h == nil {
			break
		}
		switch h.kind {
		case 'D':
			if h.describePortal {
				if next := s.nextExec(); next != nil {
					next.stmt.columns, next.stmt.formats = columns(&m)
				} else {
					s.nextRowDesc = &m
				}
			}
			s.pop()
		case 'Q':
			h.stmt.columns, h.stmt.formats = columns(&m)
		}
	case 'n': // NoData
		s.popKind('D')
	case 'D': // DataRow
		if h != nil && (h.kind == 'E' || h.kind == 'Q') {
			s.addRow(h.stmt, body)
		}
	case 'C': // CommandComplete
		var m pgproto3.CommandComplete
		tag := ""
		if m.Decode(body) == nil {
			tag = string(m.CommandTag)
		}
		switch {
		case h != nil && h.kind == 'E':
			h.stmt.tag = tag
			s.finish(h.stmt)
			s.pop()
		case h != nil && h.kind == 'Q':
			if h.stmt.tag != "" {
				h.stmt.tag += "; " + tag
			} else {
				h.stmt.tag = tag
			}
		}
		s.lastTag = tag
	case 'I', 's': // EmptyQueryResponse, PortalSuspended
		if h != nil && h.kind == 'E' {
			s.finish(h.stmt)
			s.pop()
		}
	case 'E': // ErrorResponse
		var m pgproto3.ErrorResponse
		_ = m.Decode(body)
		s.lastTag = "ERROR"
		if h == nil {
			break
		}
		if h.kind == 'Q' {
			h.stmt.errCode, h.stmt.errMessage = m.Code, m.Message
			break // completed at ReadyForQuery
		}
		// Extended protocol: the failing message is the queue head; the server
		// then skips everything up to the next Sync.
		if h.kind == 'E' {
			h.stmt.errCode, h.stmt.errMessage = m.Code, m.Message
			s.finish(h.stmt)
		} else if next := s.nextExec(); next != nil {
			next.stmt.errCode, next.stmt.errMessage = m.Code, m.Message
			s.finish(next.stmt)
		}
		for len(s.queue) > 0 && s.queue[0].kind != 'S' {
			s.pop()
		}
	case 'Z': // ReadyForQuery
		var m pgproto3.ReadyForQuery
		status := byte('I')
		if m.Decode(body) == nil {
			status = m.TxStatus
		}
		if h != nil && h.kind == 'Q' {
			s.finish(h.stmt)
			s.pop()
		} else if h != nil && h.kind == 'S' {
			s.pop()
		}
		s.onTxStatus(status)
	}
	return true
}

// nextExec returns the next queued Execute before the next Sync, after the head.
func (s *session) nextExec() *item {
	for _, it := range s.queue[1:] {
		if it.kind == 'E' {
			return it
		}
		if it.kind == 'S' {
			return nil
		}
	}
	return nil
}

func (s *session) onTxStatus(status byte) {
	prev := s.txStatus
	s.txStatus = status
	if status == 'I' && prev != 'I' && s.curTxn != "" {
		outcome := "ROLLBACK"
		if strings.HasPrefix(s.lastTag, "COMMIT") {
			outcome = "COMMIT"
		}
		s.w.Emit("txn_end", map[string]any{
			"conn": s.id, "txn": s.curTxn, "trace_id": s.txnTraceID, "outcome": outcome, "xid": s.pendingXid,
		})
		s.curTxn, s.txnTraceID, s.pendingXid = "", "", ""
	}
}

func (s *session) addRow(rec *stmtRec, body []byte) {
	rec.rowsSeen++
	if len(rec.rows) >= s.cfg.maxRows {
		rec.truncated = true
		return
	}
	var m pgproto3.DataRow
	if m.Decode(body) != nil {
		return
	}
	row := make([]any, len(m.Values))
	for i, v := range m.Values {
		binary := i < len(rec.formats) && rec.formats[i] == 1
		row[i] = valueOut(v, binary)
	}
	rec.rows = append(rec.rows, row)
}

func (s *session) finish(rec *stmtRec) {
	if rec.tDoneNs != 0 {
		return
	}
	rec.tDoneNs = ndjson.Now()
	s.finished++
	s.w.Emit("stmt", map[string]any{
		"conn": rec.conn, "seq": rec.seq, "protocol": rec.protocol, "sql": rec.sql, "sql_norm": rec.sqlNorm,
		"params": rec.params, "trace_id": rec.traceID, "span_id": rec.spanID, "txn": rec.txn,
		"t_send_ns": rec.tSendNs, "t_done_ns": rec.tDoneNs, "columns": rec.columns, "rows": rec.rows,
		"rows_seen": rec.rowsSeen, "rows_truncated": rec.truncated, "tag": rec.tag,
		"error_code": rec.errCode, "error_message": rec.errMessage,
	})
}

func columns(m *pgproto3.RowDescription) ([]string, []int16) {
	names := make([]string, len(m.Fields))
	formats := make([]int16, len(m.Fields))
	for i, f := range m.Fields {
		names[i] = string(f.Name)
		formats[i] = f.Format
	}
	return names, formats
}

func decodeParams(m *pgproto3.Bind) []any {
	out := make([]any, len(m.Parameters))
	for i, p := range m.Parameters {
		format := int16(0)
		switch {
		case len(m.ParameterFormatCodes) == 1:
			format = m.ParameterFormatCodes[0]
		case i < len(m.ParameterFormatCodes):
			format = m.ParameterFormatCodes[i]
		}
		out[i] = valueOut(p, format == 1)
	}
	return out
}

// valueOut renders a wire value: nil for SQL NULL, text as-is, binary as \x-hex.
func valueOut(v []byte, binary bool) any {
	if v == nil {
		return nil
	}
	if binary {
		return `\x` + hex.EncodeToString(v)
	}
	return string(v)
}
