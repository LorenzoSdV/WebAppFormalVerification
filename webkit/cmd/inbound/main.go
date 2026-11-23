// wk-inbound is a reverse proxy in front of the application.
//
// Every request gets a W3C traceparent (kept if the client sent a valid one,
// otherwise generated), which the app's OpenTelemetry instrumentation continues
// and the database driver writes into each SQL comment. The trace id is the
// request's actor id across all recorders.
//
// It records a "request" event when the request arrives (so overlapping
// in-flight requests are visible even if one never completes) and a "response"
// event when it finishes, including whether the client gave up first.
//
// Configuration (environment):
//
//	WK_LISTEN    listen address               (default ":8080")
//	WK_UPSTREAM  application base URL         (default "http://app:3000")
//	WK_OUT       output NDJSON file           (default "/wk/out/inbound.ndjson")
//	WK_MAX_BODY  bytes of each body recorded  (default 65536)
package main

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"io"
	"log"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"regexp"
	"strconv"

	"github.com/LorenzoSdV/WebAppFormalVerification/webkit/internal/ndjson"
)

type traceKey struct{}

var traceparentRe = regexp.MustCompile(`^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$`)

func env(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func randomHex(n int) string {
	b := make([]byte, n)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

// capture records the status and the first maxBody bytes of the response.
type capture struct {
	http.ResponseWriter
	status  int
	body    bytes.Buffer
	size    int64
	maxBody int
}

func (c *capture) WriteHeader(code int) {
	if c.status == 0 {
		c.status = code
	}
	c.ResponseWriter.WriteHeader(code)
}

func (c *capture) Write(p []byte) (int, error) {
	if c.status == 0 {
		c.status = http.StatusOK
	}
	if room := c.maxBody - c.body.Len(); room > 0 {
		c.body.Write(p[:min(room, len(p))])
	}
	n, err := c.ResponseWriter.Write(p)
	c.size += int64(n)
	return n, err
}

func (c *capture) Flush() {
	if f, ok := c.ResponseWriter.(http.Flusher); ok {
		f.Flush()
	}
}

func headerMap(h http.Header) map[string]string {
	out := make(map[string]string, len(h))
	for k, v := range h {
		if len(v) == 1 {
			out[k] = v[0]
		} else {
			out[k] = fmtList(v)
		}
	}
	return out
}

func fmtList(v []string) string {
	var b bytes.Buffer
	for i, s := range v {
		if i > 0 {
			b.WriteString(", ")
		}
		b.WriteString(s)
	}
	return b.String()
}

func main() {
	maxBody, err := strconv.Atoi(env("WK_MAX_BODY", "65536"))
	if err != nil {
		log.Fatalf("WK_MAX_BODY: %v", err)
	}
	upstream, err := url.Parse(env("WK_UPSTREAM", "http://app:3000"))
	if err != nil {
		log.Fatalf("WK_UPSTREAM: %v", err)
	}
	listen := env("WK_LISTEN", ":8080")
	w, err := ndjson.Open(env("WK_OUT", "/wk/out/inbound.ndjson"), "inbound")
	if err != nil {
		log.Fatal(err)
	}

	proxy := httputil.NewSingleHostReverseProxy(upstream)
	proxy.ErrorHandler = func(rw http.ResponseWriter, r *http.Request, err error) {
		traceID, _ := r.Context().Value(traceKey{}).(string)
		w.Emit("upstream_error", map[string]any{"trace_id": traceID, "error": err.Error()})
		rw.WriteHeader(http.StatusBadGateway)
	}

	handler := http.HandlerFunc(func(rw http.ResponseWriter, r *http.Request) {
		traceID := ""
		if m := traceparentRe.FindStringSubmatch(r.Header.Get("traceparent")); m != nil && m[2] != "00000000000000000000000000000000" {
			traceID = m[2]
		} else {
			traceID = randomHex(16)
			r.Header.Set("traceparent", "00-"+traceID+"-"+randomHex(8)+"-01")
		}
		r = r.WithContext(context.WithValue(r.Context(), traceKey{}, traceID))

		var reqBody []byte
		if r.Body != nil {
			head, _ := io.ReadAll(io.LimitReader(r.Body, int64(maxBody)))
			reqBody = head
			r.Body = io.NopCloser(io.MultiReader(bytes.NewReader(head), r.Body))
		}
		tStart := ndjson.Now()
		w.Emit("request", map[string]any{
			"trace_id": traceID, "method": r.Method, "path": r.URL.RequestURI(),
			"headers": headerMap(r.Header), "body": string(reqBody), "remote": r.RemoteAddr,
			"t_start_ns": tStart,
		})

		c := &capture{ResponseWriter: rw, maxBody: maxBody}
		proxy.ServeHTTP(c, r)

		w.Emit("response", map[string]any{
			"trace_id": traceID, "method": r.Method, "path": r.URL.RequestURI(),
			"status": c.status, "headers": headerMap(rw.Header()), "body": c.body.String(),
			"body_bytes": c.size, "t_start_ns": tStart, "t_end_ns": ndjson.Now(),
			"client_aborted": r.Context().Err() != nil,
		})
	})

	w.Emit("ready", map[string]any{"listen": listen, "upstream": upstream.String()})
	log.Printf("wk-inbound listening on %s -> %s", listen, upstream)
	log.Fatal(http.ListenAndServe(listen, handler))
}
