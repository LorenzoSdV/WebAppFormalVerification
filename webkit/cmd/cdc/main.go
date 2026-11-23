// wk-cdc records every committed row change from PostgreSQL logical decoding.
//
// It creates a publication for all tables and a temporary pgoutput replication
// slot, then writes one "txn" event per committed transaction: its xid, commit
// LSN (the total order of writes), commit time, and the row changes in order.
// The xid joins a CDC transaction to the proxy transaction that produced it.
//
// Requires wal_level=logical on the server.
//
// Configuration (environment):
//
//	WK_DB_URL     postgres connection URL (required)
//	WK_OUT        output NDJSON file      (default "/wk/out/cdc.ndjson")
//	WK_SLOT       replication slot name   (default "wk_slot")
//	WK_PUB        publication name        (default "wk_pub")
package main

import (
	"context"
	"errors"
	"fmt"
	"log"
	"os"
	"time"

	"github.com/jackc/pglogrepl"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgproto3"

	"github.com/LorenzoSdV/WebAppFormalVerification/webkit/internal/ndjson"
)

func env(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

type change struct {
	Op    string         `json:"op"`
	Table string         `json:"table"`
	New   map[string]any `json:"new,omitempty"`
	Old   map[string]any `json:"old,omitempty"`
}

type txn struct {
	xid        uint32
	finalLSN   pglogrepl.LSN
	beginTime  time.Time
	changes    []change
	truncTable []string
}

func main() {
	dbURL := os.Getenv("WK_DB_URL")
	if dbURL == "" {
		log.Fatal("WK_DB_URL is required")
	}
	slot := env("WK_SLOT", "wk_slot")
	pub := env("WK_PUB", "wk_pub")
	w, err := ndjson.Open(env("WK_OUT", "/wk/out/cdc.ndjson"), "cdc")
	if err != nil {
		log.Fatal(err)
	}
	ctx := context.Background()

	if err := ensurePublication(ctx, dbURL, pub); err != nil {
		w.Emit("error", map[string]any{"stage": "publication", "error": err.Error()})
		log.Fatal(err)
	}

	conn, err := connectRetry(ctx, dbURL+sep(dbURL)+"replication=database")
	if err != nil {
		log.Fatal(err)
	}
	defer conn.Close(ctx)

	sys, err := pglogrepl.IdentifySystem(ctx, conn)
	if err != nil {
		log.Fatalf("identify system: %v", err)
	}
	if _, err := pglogrepl.CreateReplicationSlot(ctx, conn, slot, "pgoutput",
		pglogrepl.CreateReplicationSlotOptions{Temporary: true, Mode: pglogrepl.LogicalReplication}); err != nil {
		w.Emit("error", map[string]any{"stage": "create_slot", "error": err.Error()})
		log.Fatalf("create slot (is wal_level=logical?): %v", err)
	}
	if err := pglogrepl.StartReplication(ctx, conn, slot, sys.XLogPos, pglogrepl.StartReplicationOptions{
		PluginArgs: []string{"proto_version '1'", fmt.Sprintf("publication_names '%s'", pub)},
	}); err != nil {
		log.Fatalf("start replication: %v", err)
	}
	w.Emit("ready", map[string]any{"slot": slot, "publication": pub, "start_lsn": sys.XLogPos.String()})
	log.Printf("wk-cdc streaming from %s", sys.XLogPos)

	relations := map[uint32]*pglogrepl.RelationMessage{}
	var cur *txn
	pos := sys.XLogPos
	nextStatus := time.Now()

	for {
		if time.Now().After(nextStatus) {
			if err := pglogrepl.SendStandbyStatusUpdate(ctx, conn, pglogrepl.StandbyStatusUpdate{
				WALWritePosition: pos, WALFlushPosition: pos, WALApplyPosition: pos,
			}); err != nil {
				log.Fatalf("status update: %v", err)
			}
			nextStatus = time.Now().Add(5 * time.Second)
		}

		rctx, cancel := context.WithDeadline(ctx, nextStatus)
		raw, err := conn.ReceiveMessage(rctx)
		cancel()
		if err != nil {
			if pgconn.Timeout(err) {
				continue
			}
			log.Fatalf("receive: %v", err)
		}
		if errMsg, ok := raw.(*pgproto3.ErrorResponse); ok {
			log.Fatalf("server error: %s", errMsg.Message)
		}
		cd, ok := raw.(*pgproto3.CopyData)
		if !ok || len(cd.Data) == 0 {
			continue
		}

		switch cd.Data[0] {
		case pglogrepl.PrimaryKeepaliveMessageByteID:
			ka, err := pglogrepl.ParsePrimaryKeepaliveMessage(cd.Data[1:])
			if err == nil && ka.ReplyRequested {
				nextStatus = time.Time{}
			}
		case pglogrepl.XLogDataByteID:
			xld, err := pglogrepl.ParseXLogData(cd.Data[1:])
			if err != nil {
				log.Fatalf("parse xlogdata: %v", err)
			}
			msg, err := pglogrepl.Parse(xld.WALData)
			if err != nil {
				log.Fatalf("parse logical message: %v", err)
			}
			cur = handle(w, msg, relations, cur)
			if end := xld.WALStart + pglogrepl.LSN(len(xld.WALData)); end > pos {
				pos = end
			}
		}
	}
}

func handle(w *ndjson.Writer, msg pglogrepl.Message, rels map[uint32]*pglogrepl.RelationMessage, cur *txn) *txn {
	switch m := msg.(type) {
	case *pglogrepl.RelationMessage:
		rels[m.RelationID] = m
	case *pglogrepl.BeginMessage:
		return &txn{xid: m.Xid, finalLSN: m.FinalLSN, beginTime: m.CommitTime}
	case *pglogrepl.InsertMessage:
		if cur != nil {
			rel := rels[m.RelationID]
			cur.changes = append(cur.changes, change{Op: "insert", Table: tableName(rel), New: tuple(rel, m.Tuple)})
		}
	case *pglogrepl.UpdateMessage:
		if cur != nil {
			rel := rels[m.RelationID]
			cur.changes = append(cur.changes, change{Op: "update", Table: tableName(rel),
				New: tuple(rel, m.NewTuple), Old: tuple(rel, m.OldTuple)})
		}
	case *pglogrepl.DeleteMessage:
		if cur != nil {
			rel := rels[m.RelationID]
			cur.changes = append(cur.changes, change{Op: "delete", Table: tableName(rel), Old: tuple(rel, m.OldTuple)})
		}
	case *pglogrepl.TruncateMessage:
		if cur != nil {
			for _, id := range m.RelationIDs {
				cur.changes = append(cur.changes, change{Op: "truncate", Table: tableName(rels[id])})
			}
		}
	case *pglogrepl.CommitMessage:
		if cur == nil {
			return nil
		}
		w.Emit("txn", map[string]any{
			"xid":            cur.xid,
			"commit_lsn":     m.CommitLSN.String(),
			"commit_lsn_int": uint64(m.CommitLSN),
			"commit_time_ns": m.CommitTime.UnixNano(),
			"changes":        cur.changes,
		})
		return nil
	}
	return cur
}

func tableName(rel *pglogrepl.RelationMessage) string {
	if rel == nil {
		return "?"
	}
	if rel.Namespace == "" || rel.Namespace == "public" {
		return rel.RelationName
	}
	return rel.Namespace + "." + rel.RelationName
}

// tuple decodes a pgoutput tuple to column name -> text value. Unchanged
// TOASTed values are reported as {"unchanged_toast": true}.
func tuple(rel *pglogrepl.RelationMessage, t *pglogrepl.TupleData) map[string]any {
	if rel == nil || t == nil {
		return nil
	}
	out := make(map[string]any, len(t.Columns))
	for i, col := range t.Columns {
		name := fmt.Sprintf("col%d", i)
		if i < len(rel.Columns) {
			name = rel.Columns[i].Name
		}
		switch col.DataType {
		case pglogrepl.TupleDataTypeNull:
			out[name] = nil
		case pglogrepl.TupleDataTypeToast:
			out[name] = map[string]any{"unchanged_toast": true}
		default:
			out[name] = string(col.Data)
		}
	}
	return out
}

func ensurePublication(ctx context.Context, url, pub string) error {
	conn, err := connectRetry(ctx, url)
	if err != nil {
		return err
	}
	defer conn.Close(ctx)
	_, err = conn.Exec(ctx, fmt.Sprintf("CREATE PUBLICATION %s FOR ALL TABLES", pgQuoteIdent(pub))).ReadAll()
	var pgErr *pgconn.PgError
	if errors.As(err, &pgErr) && pgErr.Code == "42710" { // duplicate_object
		return nil
	}
	return err
}

func connectRetry(ctx context.Context, url string) (*pgconn.PgConn, error) {
	var lastErr error
	for i := 0; i < 60; i++ {
		conn, err := pgconn.Connect(ctx, url)
		if err == nil {
			return conn, nil
		}
		lastErr = err
		time.Sleep(time.Second)
	}
	return nil, fmt.Errorf("connect: %w", lastErr)
}

func sep(url string) string {
	for _, c := range url {
		if c == '?' {
			return "&"
		}
	}
	return "?"
}

func pgQuoteIdent(s string) string {
	out := []rune{'"'}
	for _, c := range s {
		if c == '"' {
			out = append(out, '"')
		}
		out = append(out, c)
	}
	return string(append(out, '"'))
}
