package agentproc

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"time"
)

// HistoryEntry is one line of a session's JSONL history (a convenience the
// Python SDK also ships; storage shape is SDK-local, not wire).
type HistoryEntry struct {
	Role      string `json:"role"`
	Content   string `json:"content"`
	Timestamp string `json:"timestamp"`
}

// SessionFilePath resolves the JSONL history file path for a session,
// creating the sessions directory if needed. Returns an error when
// sessionID is empty or not a storage-safe filename component (see
// IsValidSessionIDString — a "/" in the id would path-traverse out of the
// sessions directory).
func SessionFilePath(sessionID string, baseDir string) (string, error) {
	if sessionID == "" {
		return "", fmt.Errorf("session_id must be non-empty")
	}
	if !IsValidSessionIDString(sessionID) {
		return "", fmt.Errorf("session_id is not a safe filename component: %q", sessionID)
	}
	root := baseDir
	if root == "" {
		home, err := os.UserHomeDir()
		if err != nil {
			return "", err
		}
		root = filepath.Join(home, ".agentproc", "sessions")
	}
	if err := os.MkdirAll(root, 0o755); err != nil {
		return "", err
	}
	return filepath.Join(root, sessionID+".jsonl"), nil
}

// LoadHistory loads the conversation history for a session; nil when the
// session is empty or no file exists yet.
func LoadHistory(sessionID string, baseDir string) ([]HistoryEntry, error) {
	if sessionID == "" {
		return nil, nil
	}
	path, err := SessionFilePath(sessionID, baseDir)
	if err != nil {
		return nil, nil
	}
	f, err := os.Open(path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, err
	}
	defer f.Close()
	var entries []HistoryEntry
	scanner := bufio.NewScanner(f)
	scanner.Buffer(make([]byte, 0, 64*1024), 8*1024*1024)
	for scanner.Scan() {
		line := scanner.Text()
		if line == "" {
			continue
		}
		var e HistoryEntry
		if json.Unmarshal([]byte(line), &e) != nil {
			continue
		}
		entries = append(entries, e)
	}
	return entries, scanner.Err()
}

// AppendHistory appends entries to a session's JSONL history file. A no-op
// when sessionID is empty or entries is empty.
func AppendHistory(sessionID string, entries []HistoryEntry, baseDir string) error {
	if sessionID == "" || len(entries) == 0 {
		return nil
	}
	path, err := SessionFilePath(sessionID, baseDir)
	if err != nil {
		return err
	}
	f, err := os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		return err
	}
	defer f.Close()
	for _, e := range entries {
		if e.Timestamp == "" {
			e.Timestamp = time.Now().UTC().Format(time.RFC3339Nano)
		}
		line, err := json.Marshal(e)
		if err != nil {
			return err
		}
		if _, err := f.Write(append(line, '\n')); err != nil {
			return err
		}
	}
	return nil
}
