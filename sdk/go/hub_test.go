package agentproc

// Hub client tests against a local fake jsDelivr (httptest): the fetch/cache
// flow, ref pinning with cache invalidation, the did-you-mean hint, and the
// install/copy semantics. The CDN base URLs are package vars, so no network.

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// fakeHub serves the jsDelivr raw + data API shapes for a small tree.
func fakeHub(t *testing.T) *httptest.Server {
	t.Helper()
	mux := http.NewServeMux()
	raw := func(w http.ResponseWriter, path string, body string, status int) {
		w.WriteHeader(status)
		_, _ = w.Write([]byte(body))
	}
	mux.HandleFunc("/jeffkit/agentproc@main/hub/echo-agent/profile.yaml", func(w http.ResponseWriter, r *http.Request) {
		raw(w, "", "name: echo-agent\ndescription: Echoes your message back\ntested: verified\n", 200)
	})
	mux.HandleFunc("/jeffkit/agentproc@main/hub/echo-agent/README.md", func(w http.ResponseWriter, r *http.Request) {
		raw(w, "", "# echo-agent\n", 200)
	})
	mux.HandleFunc("/jeffkit/agentproc@main/hub/echo-agent/bridge.py", func(w http.ResponseWriter, r *http.Request) {
		raw(w, "", "print('bridge')\n", 200)
	})
	mux.HandleFunc("/jeffkit/agentproc@main/hub/echo-agent/bridge.sh", func(w http.ResponseWriter, r *http.Request) {
		raw(w, "", "#!/usr/bin/env bash\n", 200)
	})
	mux.HandleFunc("/jeffkit/agentproc@main/hub/_shared/stream_utils.py", func(w http.ResponseWriter, r *http.Request) {
		raw(w, "", "def spawn_cli_session(): ...\n", 200)
	})
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		// data API tree + raw 404 fallback for unknown profile files
		if strings.HasPrefix(r.URL.Path, "/v1/packages/gh/jeffkit/agentproc@") {
			tree := map[string]any{"files": []any{
				map[string]any{"name": "hub", "type": "directory", "files": []any{
					map[string]any{"name": "echo-agent", "type": "directory", "files": []any{}},
					map[string]any{"name": "_shared", "type": "directory", "files": []any{}},
					map[string]any{"name": "README.md", "type": "file"},
				}},
			}}
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(tree)
			return
		}
		raw(w, "", "not found", 404)
	})
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)

	// Point the client at the fake and isolate the cache. The client builds
	// <base>/<repo>@<ref>/... — the default ref (main) hits the mux paths
	// registered above. HOME is isolated so the disk cache never touches the
	// developer's real ~/.agentproc.
	t.Setenv("HOME", t.TempDir())
	t.Setenv("AGENTPROC_HUB_REF", "")
	jsdelivrRawBase = srv.URL
	jsdelivrDataBase = srv.URL + "/v1/packages/gh"
	clearTreeCache()
	t.Cleanup(func() {
		jsdelivrRawBase = "https://cdn.jsdelivr.net/gh"
		jsdelivrDataBase = "https://data.jsdelivr.com/v1/packages/gh"
		clearTreeCache()
	})
	return srv
}

func TestFetchProfileCachesAndServes(t *testing.T) {
	fakeHub(t)
	var logs []string
	dir, herr := FetchProfile("echo-agent", false, func(l string) { logs = append(logs, l) })
	if herr != nil {
		t.Fatalf("fetch: %s (%s)", herr.Message, herr.Hint)
	}
	for _, f := range []string{"profile.yaml", "bridge.py", "bridge.sh", "README.md"} {
		if _, err := os.Stat(filepath.Join(dir, f)); err != nil {
			t.Fatalf("cached file %s missing: %v", f, err)
		}
	}
	// _shared is populated alongside for the bridge imports.
	if _, err := os.Stat(filepath.Join(HubCacheDir("_shared"), "stream_utils.py")); err != nil {
		t.Fatalf("_shared not cached: %v", err)
	}
	// Second fetch within TTL: no CDN traffic (verified by breaking the
	// server) — the fresh-cache path returns before any HTTP call.
	original := jsdelivrRawBase
	jsdelivrRawBase = "http://127.0.0.1:1/unreachable"
	defer func() { jsdelivrRawBase = original }()
	again, herr := FetchProfile("echo-agent", false, nil)
	if herr != nil || again != dir {
		t.Fatalf("fresh cache must serve without network: %v", herr)
	}
}

func TestFetchProfileUnknownNameSuggests(t *testing.T) {
	fakeHub(t)
	_, herr := FetchProfile("echo", false, nil)
	if herr == nil || herr.Status != 404 {
		t.Fatalf("expected 404 HubError, got %+v", herr)
	}
	if !strings.Contains(herr.Hint, "Did you mean `echo-agent`?") {
		t.Fatalf("did-you-mean hint missing: %s", herr.Hint)
	}
	if !strings.Contains(herr.Hint, "Available profiles:") {
		t.Fatalf("available list missing: %s", herr.Hint)
	}
}

func TestFetchProfileRefPinningInvalidatesCache(t *testing.T) {
	fakeHub(t)
	dir, herr := FetchProfile("echo-agent", false, nil)
	if herr != nil {
		t.Fatalf("fetch: %v", herr)
	}
	// Pin a different ref: the cache must count as a miss and be refetched
	// (the fake serves the same bytes; the point is the meta flips).
	t.Setenv("AGENTPROC_HUB_REF", "v9.9.9")
	clearTreeCache()
	// The fake mux only has @main paths, so a v9.9.9 raw fetch 404s → the
	// probe misses → "not found in hub". That proves the ref change reached
	// the client instead of serving the cached main copy.
	_, herr = FetchProfile("echo-agent", false, nil)
	if herr == nil || herr.Status != 404 {
		t.Fatalf("pinned ref must not serve the main cache: %+v", herr)
	}
	_ = dir
}

func TestListProfiles(t *testing.T) {
	fakeHub(t)
	profiles, herr := ListProfiles(false, nil)
	if herr != nil {
		t.Fatalf("list: %v", herr)
	}
	if len(profiles) != 1 || profiles[0].Name != "echo-agent" || profiles[0].Tested != "verified" {
		t.Fatalf("unexpected profiles: %+v", profiles)
	}
	// _-prefixed utility dirs are excluded; plain files (hub/README.md)
	// are not profiles.
	for _, p := range profiles {
		if strings.HasPrefix(p.Name, "_") || p.Name == "README.md" {
			t.Fatalf("non-profile entry leaked: %s", p.Name)
		}
	}
}

func TestShowReadmeAndInstall(t *testing.T) {
	fakeHub(t)
	readme, herr := ShowReadme("echo-agent", false, nil)
	if herr != nil || !strings.Contains(readme, "# echo-agent") {
		t.Fatalf("readme: %q err=%v", readme, herr)
	}
	target := t.TempDir()
	dest, herr := InstallProfile("echo-agent", target, false, nil)
	if herr != nil {
		t.Fatalf("install: %v", herr)
	}
	if _, err := os.Stat(filepath.Join(dest, "profile.yaml")); err != nil {
		t.Fatalf("installed profile missing: %v", err)
	}
	if _, err := os.Stat(filepath.Join(target, "_shared", "stream_utils.py")); err != nil {
		t.Fatalf("installed _shared missing: %v", err)
	}
	// Installing over an existing target is refused.
	if _, herr := InstallProfile("echo-agent", target, false, nil); herr == nil {
		t.Fatal("second install must be refused")
	}
}

func TestSuggestCloseName(t *testing.T) {
	candidates := []string{"claude-code", "echo-agent", "gemini-cli", "codex"}
	cases := []struct{ in, want string }{
		{"claude", "claude-code"}, // unambiguous prefix
		{"echo", "echo-agent"},
		{"codx", "codex"}, // 1 edit
		{"nothing-matches-this-at-all", ""},
	}
	for _, c := range cases {
		if got := suggestCloseName(c.in, candidates); got != c.want {
			t.Fatalf("suggest(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}
