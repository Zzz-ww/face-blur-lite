// gocp/main.go
//
// Go 控制面服务——docs/control-plane-contract.md 契约的 HTTP 形态。
// 业界分工复刻：平台控制面 Go + 算法数据面 Python（K8s/Temporal 同款）。
// 轻量约束：单文件、stdlib net/http + 唯一依赖 modernc.org/sqlite
// （纯 Go 无 CGO）、无框架/无 gRPC/无容器。与 Python 端
// SqliteControlPlane 同 schema、同事务语义，过同一份契约测试。
//
// 用法：gocp <db_path> <listen_addr>   默认 pipeline.db 127.0.0.1:8080
package main

import (
	"crypto/rand"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"log"
	"net/http"
	"os"
	"time"

	_ "modernc.org/sqlite"
)

var db *sql.DB

// ---- 小工具 ----

func newID() string {
	b := make([]byte, 6)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

func nowF() float64 { return float64(time.Now().UnixNano()) / 1e9 }

func writeJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, code int, msg string) {
	// 头必须在 WriteHeader 之前设置——写在后面永远不生效
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	writeJSON(w, map[string]string{"error": msg})
}

// 契约 §6：可空列必须显式置 nil（Python dict[row] 的 None 语义，不缺键）
func setNull(m map[string]any, k string, v sql.NullString) {
	if v.Valid {
		m[k] = v.String
	} else {
		m[k] = nil
	}
}

func setNullF(m map[string]any, k string, v sql.NullFloat64) {
	if v.Valid {
		m[k] = v.Float64
	} else {
		m[k] = nil
	}
}

// scanChunk：SELECT * 的 chunks 行 → map（键名 = 列名，与 Python 一致）
func scanChunk(scan func(dest ...any) error) (map[string]any, error) {
	var id, videoID, in, out, status string
	var idx, attempt int
	var workerID, metrics, errStr sql.NullString
	var leaseUntil, enqueuedAt, startedAt, finishedAt sql.NullFloat64
	if err := scan(&id, &videoID, &idx, &in, &out, &status, &attempt,
		&workerID, &leaseUntil, &metrics, &enqueuedAt, &startedAt,
		&finishedAt, &errStr); err != nil {
		return nil, err
	}
	m := map[string]any{"id": id, "video_id": videoID, "idx": idx,
		"input_path": in, "output_path": out, "status": status,
		"attempt": attempt, "enqueued_at": enqueuedAt.Float64}
	setNull(m, "worker_id", workerID)
	setNull(m, "metrics", metrics)
	setNull(m, "error", errStr)
	setNullF(m, "lease_until", leaseUntil)
	setNullF(m, "started_at", startedAt)
	setNullF(m, "finished_at", finishedAt)
	return m, nil
}

func getVideo(id string) (map[string]any, error) {
	var in, out, status string
	var merged, errStr sql.NullString
	var createdAt float64
	var finishedAt sql.NullFloat64
	err := db.QueryRow(`SELECT input_path, output_path, merged_path,
		status, error, created_at, finished_at FROM videos WHERE id=?`, id).
		Scan(&in, &out, &merged, &status, &errStr, &createdAt, &finishedAt)
	if err != nil {
		return nil, err
	}
	m := map[string]any{"id": id, "input_path": in, "output_path": out,
		"status": status, "created_at": createdAt}
	setNull(m, "merged_path", merged)
	setNull(m, "error", errStr)
	setNullF(m, "finished_at", finishedAt)
	return m, nil
}

// ---- schema：与 state.py 逐字一致（契约 §1） ----

const ddl = `
CREATE TABLE IF NOT EXISTS videos(
  id TEXT PRIMARY KEY,
  input_path TEXT NOT NULL,
  output_path TEXT NOT NULL,
  merged_path TEXT,
  status TEXT NOT NULL,
  error TEXT,
  created_at REAL NOT NULL,
  finished_at REAL);
CREATE TABLE IF NOT EXISTS chunks(
  id TEXT PRIMARY KEY,
  video_id TEXT NOT NULL REFERENCES videos(id),
  idx INTEGER NOT NULL,
  input_path TEXT NOT NULL,
  output_path TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  attempt INTEGER NOT NULL DEFAULT 0,
  worker_id TEXT,
  lease_until REAL,
  metrics TEXT,
  enqueued_at REAL NOT NULL,
  started_at REAL,
  finished_at REAL,
  error TEXT,
  UNIQUE(video_id, idx));`

// ---- handlers：每个写操作一个事务（契约 §3） ----

func handleSubmit(w http.ResponseWriter, r *http.Request) {
	var req struct {
		InputPath   string  `json:"input_path"`
		FinalOutput string  `json:"final_output"`
		ChunkInputs [][]any `json:"chunk_inputs"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, 400, err.Error())
		return
	}
	vid, now := newID(), nowF()
	tx, err := db.Begin()
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	defer tx.Rollback()
	if _, err = tx.Exec(`INSERT INTO videos(id,input_path,output_path,
		merged_path,status,error,created_at,finished_at)
		VALUES(?,?,?,NULL,'processing',NULL,?,NULL)`,
		vid, req.InputPath, req.FinalOutput, now); err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	for _, c := range req.ChunkInputs {
		if len(c) != 3 {
			writeErr(w, 400, "chunk_inputs 项必须是 [idx, input, output]")
			return
		}
		idx, _ := c[0].(float64)
		in, _ := c[1].(string)
		out, _ := c[2].(string)
		if _, err = tx.Exec(`INSERT INTO chunks(id,video_id,idx,input_path,
			output_path,status,attempt,worker_id,lease_until,metrics,
			enqueued_at,started_at,finished_at,error)
			VALUES(?,?,?,?,?,'pending',0,NULL,NULL,NULL,?,NULL,NULL,NULL)`,
			newID(), vid, int(idx), in, out, now); err != nil {
			writeErr(w, 500, err.Error())
			return
		}
	}
	if err = tx.Commit(); err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	writeJSON(w, map[string]string{"video_id": vid})
}

func handleClaim(w http.ResponseWriter, r *http.Request) {
	var req struct {
		WorkerID string  `json:"worker_id"`
		TtlS     float64 `json:"ttl_s"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, 400, err.Error())
		return
	}
	tx, err := db.Begin()
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	defer tx.Rollback()
	row := tx.QueryRow(`SELECT c.* FROM chunks c
		JOIN videos v ON c.video_id = v.id
		WHERE c.status='pending' AND v.status='processing'
		ORDER BY v.created_at, c.idx LIMIT 1`)
	ch, err := scanChunk(row.Scan)
	if errors.Is(err, sql.ErrNoRows) {
		writeJSON(w, nil) // JSON null → Python None
		return
	}
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	now, lease := nowF(), nowF()+req.TtlS
	if _, err = tx.Exec(`UPDATE chunks SET status='running', worker_id=?,
		lease_until=?, started_at=? WHERE id=? AND status='pending'`,
		req.WorkerID, lease, now, ch["id"]); err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	if err = tx.Commit(); err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	ch["status"], ch["worker_id"], ch["lease_until"] =
		"running", req.WorkerID, lease
	writeJSON(w, ch)
}

func handleComplete(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Metrics map[string]any `json:"metrics"`
	}
	_ = json.NewDecoder(r.Body).Decode(&req)
	metrics, _ := json.Marshal(req.Metrics)
	tx, err := db.Begin()
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	defer tx.Rollback()
	var videoID string
	if err = tx.QueryRow("SELECT video_id FROM chunks WHERE id=?",
		r.PathValue("id")).Scan(&videoID); err != nil {
		// DB 错误 ≠ 不存在：仅 ErrNoRows 走守卫路径，其余 500
		if !errors.Is(err, sql.ErrNoRows) {
			writeErr(w, 500, err.Error())
			return
		}
		// 缺失 id 与参考实现对齐（Python：UPDATE rowcount==0 早退返回
		// False，不抛 KeyError）——commit 空事务 + merge_claimed=false
		if err = tx.Commit(); err != nil {
			writeErr(w, 500, err.Error())
			return
		}
		writeJSON(w, map[string]bool{"merge_claimed": false})
		return
	}
	// 契约 v1.1：仅 running 态受理——迟到 ack（已重派/终态）不算数，
	// 不进入 join 计数（与 state.py complete_chunk 守卫逐字对应）
	res, err := tx.Exec(
		"UPDATE chunks SET status='done', metrics=?, finished_at=? "+
			"WHERE id=? AND status='running'",
		string(metrics), nowF(), r.PathValue("id"))
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	if n, _ := res.RowsAffected(); n == 0 {
		if err = tx.Commit(); err != nil {
			writeErr(w, 500, err.Error())
			return
		}
		writeJSON(w, map[string]bool{"merge_claimed": false})
		return
	}
	var left int
	if err = tx.QueryRow(
		"SELECT COUNT(*) FROM chunks WHERE video_id=? AND status!='done'",
		videoID).Scan(&left); err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	claimed := false
	if left == 0 { // 原子 Join：恰好一个调用者置 merging 成功
		res, err := tx.Exec("UPDATE videos SET status='merging' "+
			"WHERE id=? AND status='processing'", videoID)
		if err != nil {
			writeErr(w, 500, err.Error())
			return
		}
		if n, _ := res.RowsAffected(); n == 1 {
			claimed = true
		}
	}
	if err = tx.Commit(); err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	writeJSON(w, map[string]bool{"merge_claimed": claimed})
}

func handleFail(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Error       string `json:"error"`
		MaxAttempts int    `json:"max_attempts"`
	}
	_ = json.NewDecoder(r.Body).Decode(&req)
	if req.MaxAttempts <= 0 {
		req.MaxAttempts = 3
	}
	tx, err := db.Begin()
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	defer tx.Rollback()
	var videoID, status string
	var attempt int
	if err = tx.QueryRow("SELECT video_id, attempt, status FROM chunks WHERE id=?",
		r.PathValue("id")).Scan(&videoID, &attempt, &status); err != nil {
		// DB 错误 ≠ 不存在：仅 ErrNoRows 走守卫路径，其余 500
		if !errors.Is(err, sql.ErrNoRows) {
			writeErr(w, 500, err.Error())
			return
		}
		// 缺失 id 与参考实现对齐（Python：row is None 早退静默 no-op，
		// 不抛 KeyError）——commit 空事务 + ok=true
		if err = tx.Commit(); err != nil {
			writeErr(w, 500, err.Error())
			return
		}
		writeJSON(w, map[string]bool{"ok": true})
		return
	}
	// 契约 v1.1：终态（done/failed）不可逆——迟到 fail 不受理（静默 ok）
	if status == "done" || status == "failed" {
		if err = tx.Commit(); err != nil {
			writeErr(w, 500, err.Error())
			return
		}
		writeJSON(w, map[string]bool{"ok": true})
		return
	}
	if attempt+1 >= req.MaxAttempts { // 耗尽：chunk+video 双双 failed（attempt 记最终计数）
		if _, err = tx.Exec("UPDATE chunks SET status='failed', attempt=attempt+1, "+
			"error=?, finished_at=? WHERE id=?",
			req.Error, nowF(), r.PathValue("id")); err != nil {
			writeErr(w, 500, err.Error())
			return
		}
		if _, err = tx.Exec("UPDATE videos SET status='failed', error=?, "+
			"finished_at=? WHERE id=? AND status='processing'",
			req.Error, nowF(), videoID); err != nil {
			writeErr(w, 500, err.Error())
			return
		}
	} else { // 未耗尽：回 pending 重派，attempt+1
		if _, err = tx.Exec(`UPDATE chunks SET status='pending',
			attempt=attempt+1, worker_id=NULL, lease_until=NULL, error=?
			WHERE id=?`, req.Error, r.PathValue("id")); err != nil {
			writeErr(w, 500, err.Error())
			return
		}
	}
	if err = tx.Commit(); err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	writeJSON(w, map[string]bool{"ok": true})
}

func handleReclaim(w http.ResponseWriter, r *http.Request) {
	var req struct {
		MaxAttempts int `json:"max_attempts"`
	}
	_ = json.NewDecoder(r.Body).Decode(&req)
	if req.MaxAttempts <= 0 {
		req.MaxAttempts = 3
	}
	tx, err := db.Begin()
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	defer tx.Rollback()
	// 契约 v1.1：仅回收 video 仍 processing 的过期 chunk（终态视频的错误
	// 原因不被改写；与 claim 的 video 过滤对称）
	rows, err := tx.Query(
		`SELECT c.* FROM chunks c JOIN videos v ON c.video_id = v.id
		 WHERE c.status='running' AND c.lease_until < ?
		 AND v.status='processing'`, nowF())
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	type expired struct {
		id, videoID string
		attempt     int
	}
	var list []expired
	for rows.Next() { // 先取全再改：避免同事务内读写交错
		ch, err := scanChunk(rows.Scan)
		if err != nil {
			rows.Close()
			writeErr(w, 500, err.Error())
			return
		}
		list = append(list, expired{ch["id"].(string),
			ch["video_id"].(string), ch["attempt"].(int)})
	}
	if err := rows.Err(); err != nil { // 迭代中断的传输层错误 ≠ 查完
		rows.Close()
		writeErr(w, 500, err.Error())
		return
	}
	rows.Close()
	for _, e := range list {
		if e.attempt+1 >= req.MaxAttempts {
			// 契约 v1.1：耗尽分支 attempt+1；chunk 不写 error/finished_at
			// （有意与 fail_chunk 不同——失败原因记在 video 级，见契约 §3）
			if _, err = tx.Exec("UPDATE chunks SET status='failed', "+
				"attempt=attempt+1 WHERE id=?", e.id); err != nil {
				writeErr(w, 500, err.Error())
				return
			}
			if _, err = tx.Exec("UPDATE videos SET status='failed', "+
				"error='lease 过期且重试耗尽', finished_at=? "+
				"WHERE id=? AND status='processing'", nowF(), e.videoID); err != nil {
				writeErr(w, 500, err.Error())
				return
			}
		} else {
			if _, err = tx.Exec(`UPDATE chunks SET status='pending',
				attempt=attempt+1, worker_id=NULL, lease_until=NULL
				WHERE id=?`, e.id); err != nil {
				writeErr(w, 500, err.Error())
				return
			}
		}
	}
	if err = tx.Commit(); err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	writeJSON(w, map[string]int{"reclaimed": len(list)})
}

func handleMergeComplete(w http.ResponseWriter, r *http.Request) {
	var req struct {
		MergedPath string `json:"merged_path"`
	}
	_ = json.NewDecoder(r.Body).Decode(&req)
	if _, err := db.Exec(
		"UPDATE videos SET status='qc_wait', merged_path=? WHERE id=?",
		req.MergedPath, r.PathValue("id")); err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	writeJSON(w, map[string]bool{"ok": true})
}

func handleFinish(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Status     string  `json:"status"`
		OutputPath *string `json:"output_path"`
	}
	_ = json.NewDecoder(r.Body).Decode(&req)
	if _, err := db.Exec(
		"UPDATE videos SET status=?, finished_at=?, "+
			"output_path=COALESCE(?, output_path) WHERE id=?",
		req.Status, nowF(), req.OutputPath, r.PathValue("id")); err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	writeJSON(w, map[string]bool{"ok": true})
}

func handleGetVideo(w http.ResponseWriter, r *http.Request) {
	v, err := getVideo(r.PathValue("id"))
	if err != nil {
		writeErr(w, 404, "video 不存在")
		return
	}
	writeJSON(w, v)
}

func handleGetChunk(w http.ResponseWriter, r *http.Request) {
	// 契约 v1.1：读单 chunk 全字段（只读；不存在 404 → 客户端转 KeyError）
	row := db.QueryRow(`SELECT id, video_id, idx, input_path, output_path,
		status, attempt, worker_id, lease_until, metrics, enqueued_at,
		started_at, finished_at, error FROM chunks WHERE id=?`,
		r.PathValue("id"))
	ch, err := scanChunk(row.Scan)
	if errors.Is(err, sql.ErrNoRows) {
		writeErr(w, 404, "chunk 不存在")
		return
	}
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	writeJSON(w, ch)
}

func handleChunkOutputs(w http.ResponseWriter, r *http.Request) {
	rows, err := db.Query(
		"SELECT output_path FROM chunks WHERE video_id=? ORDER BY idx",
		r.PathValue("id"))
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	defer rows.Close()
	out := []string{}
	for rows.Next() {
		var p string
		if err := rows.Scan(&p); err != nil {
			writeErr(w, 500, err.Error())
			return
		}
		out = append(out, p)
	}
	if err := rows.Err(); err != nil { // 迭代中断的传输层错误 ≠ 查完
		writeErr(w, 500, err.Error())
		return
	}
	writeJSON(w, map[string]any{"outputs": out})
}

func handleOverview(w http.ResponseWriter, _ *http.Request) {
	rows, err := db.Query("SELECT id FROM videos ORDER BY created_at")
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	defer rows.Close()
	vids := []map[string]any{}
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			writeErr(w, 500, err.Error())
			return
		}
		v, err := getVideo(id)
		if err != nil {
			writeErr(w, 500, err.Error())
			return
		}
		vids = append(vids, v)
	}
	if err := rows.Err(); err != nil { // 迭代中断的传输层错误 ≠ 查完
		writeErr(w, 500, err.Error())
		return
	}
	writeJSON(w, vids)
}

func main() {
	dbPath, addr := "pipeline.db", "127.0.0.1:8080"
	if len(os.Args) > 1 {
		dbPath = os.Args[1]
	}
	if len(os.Args) > 2 {
		addr = os.Args[2]
	}
	// _txlock=immediate：写事务起手即 BEGIN IMMEDIATE（契约 §5，
	// 对应 Python 端语义）；WAL + busy_timeout 同 Python 端
	var err error
	db, err = sql.Open("sqlite",
		"file:"+dbPath+
			"?_txlock=immediate"+
			"&_pragma=journal_mode(WAL)"+
			"&_pragma=busy_timeout(10000)")
	if err != nil {
		log.Fatal(err)
	}
	if _, err = db.Exec(ddl); err != nil {
		log.Fatal(err)
	}
	mux := http.NewServeMux()
	mux.HandleFunc("POST /init", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, map[string]bool{"ok": true}) // schema 启动时已建
	})
	mux.HandleFunc("POST /videos", handleSubmit)
	mux.HandleFunc("POST /chunks/claim", handleClaim)
	mux.HandleFunc("POST /chunks/{id}/complete", handleComplete)
	mux.HandleFunc("POST /chunks/{id}/fail", handleFail)
	mux.HandleFunc("POST /videos/{id}/merge_complete", handleMergeComplete)
	mux.HandleFunc("POST /videos/{id}/finish", handleFinish)
	mux.HandleFunc("POST /maintenance/reclaim", handleReclaim)
	mux.HandleFunc("GET /videos/{id}", handleGetVideo)
	mux.HandleFunc("GET /chunks/{id}", handleGetChunk)
	mux.HandleFunc("GET /videos/{id}/chunk_outputs", handleChunkOutputs)
	mux.HandleFunc("GET /videos", handleOverview)
	log.Printf("gocp listening on %s (db=%s)", addr, dbPath)
	log.Fatal(http.ListenAndServe(addr, mux))
}
