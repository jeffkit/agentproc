package agentproc

// Hub client — fetch and manage profile directories from the official Hub
// (https://github.com/jeffkit/agentproc/tree/main/hub/). Port of the Python
// SDK's hub.py, with one deliberate divergence: **no bundled copy**. The
// Python/Node CLIs ship the whole hub/ directory inside the package; a Go
// module cannot embed files outside its own subtree, so the Go client always
// resolves via the jsDelivr CDN and relies on the 24-hour cache (the first
// `hub` command needs network, the common repeated case does not).
//
// Resolution order (Go): fresh cache → jsDelivr CDN. Remote-fetched profiles
// are cached at ~/.agentproc/cache/hub/<name>/ with a 24-hour TTL; the
// shared `_shared/` bridge helpers are cached alongside (the fetched bridge
// scripts import them via a sibling path). AGENTPROC_HUB_REF pins every
// fetch to a tag/branch/commit; the cache is keyed per ref (a pinned run
// never silently serves a `main` copy and vice versa).

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	"gopkg.in/yaml.v3"
)

// HubRepo is the GitHub repo id the hub profiles live in.
const HubRepo = "jeffkit/agentproc"

// HubCacheTTLSecs is the default cache freshness window (24 hours).
const HubCacheTTLSecs = 24 * 60 * 60

// jsDelivr mirrors the GitHub repo on a global CDN (Fastly), reachable where
// raw.githubusercontent.com / api.github.com are not. No token, no 60/hr
// limit. Vars (not consts) so tests can point them at a local server.
var (
	jsdelivrRawBase  = "https://cdn.jsdelivr.net/gh"
	jsdelivrDataBase = "https://data.jsdelivr.com/v1/packages/gh"
	httpGetTimeout   = 30 * time.Second
)

// HubRef returns the git ref to fetch from — AGENTPROC_HUB_REF when set
// (tag, branch or commit sha; the fetch-and-exec trust pin from the spec),
// "main" otherwise.
func HubRef() string {
	if ref := strings.TrimSpace(os.Getenv("AGENTPROC_HUB_REF")); ref != "" {
		return ref
	}
	return "main"
}

func orDefault(s, def string) string {
	if s == "" {
		return def
	}
	return s
}

// HubError is a hub fetch failure with a human-readable remediation hint.
// The CLI prints Message + Hint to stderr instead of a stack trace.
type HubError struct {
	Message string
	Hint    string
	Status  int // HTTP status; 0 for network errors
}

func (e *HubError) Error() string { return e.Message }

const networkHint = "Could not reach the hub CDN (jsDelivr). Try:\n" +
	"  1. Re-run the command (often succeeds on retry).\n" +
	"  2. If your network requires a proxy, set HTTPS_PROXY.\n" +
	"  3. To use a local checkout instead:\n" +
	`       agentproc --profile ./hub/<name>/profile.yaml --prompt "hi"`

// HubProfile is one hub entry's metadata (parsed from its profile.yaml).
type HubProfile struct {
	Name        string `json:"name"`
	Description string `json:"description"`
	CLI         string `json:"cli"`
	Tested      string `json:"tested"`
}

func hubCacheRoot() string {
	home, err := os.UserHomeDir()
	if err != nil {
		return filepath.Join(".agentproc", "cache", "hub")
	}
	return filepath.Join(home, ".agentproc", "cache", "hub")
}

// HubCacheDir is the local cache path for a profile.
func HubCacheDir(name string) string {
	return filepath.Join(hubCacheRoot(), name)
}

// cacheAgeSecs returns seconds since the cache was last written; a miss when
// the cache was written for a different hub ref than the currently effective
// one (so switching AGENTPROC_HUB_REF invalidates previous entries).
func cacheAgeSecs(name string) (float64, bool) {
	raw, err := os.ReadFile(filepath.Join(HubCacheDir(name), ".cache-meta.json"))
	if err != nil {
		return 0, false
	}
	var meta struct {
		FetchedAt float64 `json:"fetched_at"`
		Ref       string  `json:"ref"`
	}
	if json.Unmarshal(raw, &meta) != nil {
		return 0, false
	}
	if orDefault(meta.Ref, "main") != HubRef() {
		return 0, false
	}
	age := time.Since(time.Unix(int64(meta.FetchedAt), 0)).Seconds()
	if age < 0 {
		age = 0
	}
	return age, true
}

func writeCacheMeta(name string) {
	dir := HubCacheDir(name)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return
	}
	raw, _ := json.Marshal(map[string]any{"fetched_at": time.Now().Unix(), "ref": HubRef()})
	_ = os.WriteFile(filepath.Join(dir, ".cache-meta.json"), raw, 0o644)
}

func httpGet(url string) ([]byte, *HubError) {
	client := &http.Client{Timeout: httpGetTimeout}
	resp, err := client.Get(url)
	if err != nil {
		return nil, &HubError{Message: "could not reach the hub CDN", Hint: networkHint}
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, &HubError{Message: "could not read the hub CDN response", Hint: networkHint}
	}
	if resp.StatusCode >= 400 {
		return nil, &HubError{
			Message: fmt.Sprintf("fetch failed (HTTP %d) for %s", resp.StatusCode, url),
			Status:  resp.StatusCode,
			Hint:    firstN(string(body), 200),
		}
	}
	return body, nil
}

func httpGetText(url string) (string, *HubError) {
	raw, herr := httpGet(url)
	if herr != nil {
		if herr.Status == 404 {
			herr.Hint = "Profile files should exist in the hub repo. Try `agentproc hub list`."
		}
		return "", herr
	}
	return string(raw), nil
}

// httpGetTextOptional returns "" on 404 instead of raising — used for
// probing optional profile files and for the profile-exists probe.
func httpGetTextOptional(url string) (string, *HubError) {
	text, herr := httpGetText(url)
	if herr != nil && herr.Status == 404 {
		return "", nil
	}
	return text, herr
}

func firstN(s string, n int) string {
	if len(s) > n {
		return s[:n]
	}
	return s
}

// --- repo tree (jsDelivr data API), in-memory + disk cached (24h TTL) ------

type treeEntry struct {
	Path string `json:"path"`
	Type string `json:"type"` // blob | tree
}

var (
	treeCache   []treeEntry
	treeCacheMu sync.Mutex
)

func lockTree()   { treeCacheMu.Lock() }
func unlockTree() { treeCacheMu.Unlock() }

func treeCachePath() string { return filepath.Join(hubCacheRoot(), "tree.json") }

func clearTreeCache() {
	lockTree()
	treeCache = nil
	unlockTree()
	_ = os.Remove(treeCachePath())
}

// flattenJsDelivrTree flattens the nested {files:[{type:'directory',files:[…]}]}
// response into the flat {path,type} shape GitHub's Trees API returns.
func flattenJsDelivrTree(files []any, prefix string) []treeEntry {
	var out []treeEntry
	for _, f := range files {
		e, ok := f.(map[string]any)
		if !ok {
			continue
		}
		p := prefix + toString(e["name"])
		if t, _ := e["type"].(string); t == "directory" {
			out = append(out, treeEntry{Path: p, Type: "tree"})
			if sub, ok := e["files"].([]any); ok {
				out = append(out, flattenJsDelivrTree(sub, p+"/")...)
			}
		} else {
			out = append(out, treeEntry{Path: p, Type: "blob"})
		}
	}
	return out
}

func getTree() ([]treeEntry, *HubError) {
	lockTree()
	cached := treeCache
	unlockTree()
	if cached != nil {
		return cached, nil
	}

	if raw, err := os.ReadFile(treeCachePath()); err == nil {
		var meta struct {
			FetchedAt float64     `json:"fetched_at"`
			Ref       string      `json:"ref"`
			Tree      []treeEntry `json:"tree"`
		}
		if json.Unmarshal(raw, &meta) == nil &&
			time.Since(time.Unix(int64(meta.FetchedAt), 0)).Seconds() < HubCacheTTLSecs &&
			orDefault(meta.Ref, "main") == HubRef() {
			lockTree()
			treeCache = meta.Tree
			cached = treeCache
			unlockTree()
			return cached, nil
		}
	}

	url := fmt.Sprintf("%s/%s@%s", jsdelivrDataBase, HubRepo, HubRef())
	raw, herr := httpGet(url)
	if herr != nil {
		return nil, herr
	}
	var data struct {
		Files []any `json:"files"`
	}
	if json.Unmarshal(raw, &data) != nil || data.Files == nil {
		return nil, &HubError{Message: "unexpected jsDelivr data API response"}
	}
	tree := flattenJsDelivrTree(data.Files, "")
	lockTree()
	treeCache = tree
	unlockTree()

	if err := os.MkdirAll(hubCacheRoot(), 0o755); err == nil {
		diskRaw, _ := json.Marshal(map[string]any{"fetched_at": time.Now().Unix(), "ref": HubRef(), "tree": tree})
		_ = os.WriteFile(treeCachePath(), diskRaw, 0o644) // best-effort
	}
	return tree, nil
}

func listProfileNames() ([]string, *HubError) {
	tree, herr := getTree()
	if herr != nil {
		return nil, herr
	}
	seen := map[string]bool{}
	isTree := map[string]bool{}
	for _, e := range tree {
		if e.Type == "tree" {
			isTree[e.Path] = true
		}
	}
	var names []string
	for _, e := range tree {
		if !strings.HasPrefix(e.Path, "hub/") {
			continue
		}
		seg := strings.SplitN(e.Path[len("hub/"):], "/", 2)[0]
		if seg == "" || strings.HasPrefix(seg, "_") || seen[seg] {
			continue
		}
		// Only real directories are profiles — hub/README.md etc. are files.
		if !isTree["hub/"+seg] {
			continue
		}
		seen[seg] = true
		names = append(names, seg)
	}
	sort.Strings(names)
	return names, nil
}

// suggestCloseName is the "did you mean" hint: an unambiguous prefix match
// first (`claude` → `claude-code`), then edit distance with a length-scaled
// threshold (≤6 → 1 edit, 7–12 → 2, >12 → 3).
func suggestCloseName(input string, candidates []string) string {
	if input == "" || len(candidates) == 0 {
		return ""
	}
	n := strings.ToLower(input)
	var prefixMatches []string
	for _, c := range candidates {
		if strings.HasPrefix(strings.ToLower(c), n) {
			prefixMatches = append(prefixMatches, c)
		}
	}
	if len(prefixMatches) == 1 {
		return prefixMatches[0]
	}
	threshold := 1
	if len(input) > 6 {
		threshold = 2
	}
	if len(input) > 12 {
		threshold = 3
	}
	best, bestDist := "", int(^uint(0)>>1)
	for _, c := range candidates {
		d := editDistance(n, strings.ToLower(c))
		if d < bestDist {
			bestDist, best = d, c
		}
	}
	if best != "" && bestDist <= threshold {
		return best
	}
	return ""
}

func editDistance(a, b string) int {
	ar, br := []rune(a), []rune(b)
	m, n := len(ar), len(br)
	prev := make([]int, n+1)
	curr := make([]int, n+1)
	for j := 0; j <= n; j++ {
		prev[j] = j
	}
	for i := 1; i <= m; i++ {
		curr[0] = i
		for j := 1; j <= n; j++ {
			cost := 1
			if ar[i-1] == br[j-1] {
				cost = 0
			}
			curr[j] = min3(prev[j]+1, curr[j-1]+1, prev[j-1]+cost)
		}
		prev, curr = curr, prev
	}
	return prev[n]
}

func min3(a, b, c int) int {
	if b < a {
		a = b
	}
	if c < a {
		a = c
	}
	return a
}

// Every hub profile is a fixed candidate file set (see hub/README.md);
// missing files are simply absent for a given profile.
var (
	profileFileCandidates = []string{"profile.yaml", "bridge.py", "bridge.js", "bridge.sh", "README.md"}
	sharedFileCandidates  = []string{"stream_utils.py", "stream_utils.js", "README.md"}
)

func clearDir(d string) {
	_ = os.RemoveAll(d)
	_ = os.MkdirAll(d, 0o755)
}

// ensureSharedCached populates `_shared/` in the cache root — the fetched
// bridge scripts import from it via a sibling path, so it must exist
// whenever a profile is fetched.
func ensureSharedCached(refresh bool, onLog func(string)) *HubError {
	age, cached := cacheAgeSecs("_shared")
	sdir := HubCacheDir("_shared")
	if !refresh && cached && age < HubCacheTTLSecs {
		if _, err := os.Stat(filepath.Join(sdir, "stream_utils.py")); err == nil {
			return nil
		}
	}
	clearDir(sdir)
	for _, fname := range sharedFileCandidates {
		url := fmt.Sprintf("%s/%s@%s/hub/_shared/%s", jsdelivrRawBase, HubRepo, HubRef(), fname)
		text, herr := httpGetTextOptional(url)
		if herr != nil {
			return herr
		}
		if text == "" {
			continue
		}
		if err := os.WriteFile(filepath.Join(sdir, fname), []byte(text), 0o644); err != nil {
			return &HubError{Message: fmt.Sprintf("cannot write cache: %v", err)}
		}
		if onLog != nil {
			onLog("  - _shared/" + fname)
		}
	}
	writeCacheMeta("_shared")
	return nil
}

// FetchProfile fetches a profile directory to the local cache and returns
// its path. Resolution: fresh cache (younger than 24h, same hub ref) →
// jsDelivr CDN. `_shared/` is populated alongside.
func FetchProfile(name string, refresh bool, onLog func(string)) (string, *HubError) {
	if refresh {
		clearTreeCache()
	}
	age, cached := cacheAgeSecs(name)
	cachePath := HubCacheDir(name)
	profileYAML := filepath.Join(cachePath, "profile.yaml")
	if !refresh && cached && age < HubCacheTTLSecs {
		if _, err := os.Stat(profileYAML); err == nil {
			if onLog != nil {
				onLog(fmt.Sprintf("using cached profile: %s (age %ds)", cachePath, int(age)))
			}
			if herr := ensureSharedCached(false, onLog); herr != nil {
				return "", herr
			}
			return cachePath, nil
		}
	}

	if onLog != nil {
		if refresh {
			onLog(fmt.Sprintf("refreshing profile '%s' from jsDelivr CDN...", name))
		} else {
			onLog(fmt.Sprintf("fetching profile '%s' from jsDelivr CDN...", name))
		}
	}

	// Probe profile.yaml first: a 404 is a wrong name, not a fetch error.
	probeURL := fmt.Sprintf("%s/%s@%s/hub/%s/profile.yaml", jsdelivrRawBase, HubRepo, HubRef(), name)
	probe, herr := httpGetTextOptional(probeURL)
	if herr != nil {
		return "", herr
	}
	if probe == "" {
		known, _ := listProfileNames()
		var lines []string
		if suggestion := suggestCloseName(name, known); suggestion != "" {
			lines = append(lines, fmt.Sprintf("Did you mean `%s`?", suggestion), "")
		}
		lines = append(lines, "Available profiles:")
		for _, k := range known {
			lines = append(lines, "  - "+k)
		}
		return "", &HubError{
			Message: fmt.Sprintf("profile '%s' not found in hub", name),
			Status:  404,
			Hint:    strings.Join(lines, "\n"),
		}
	}

	clearDir(cachePath)
	if err := os.WriteFile(profileYAML, []byte(probe), 0o644); err != nil {
		return "", &HubError{Message: fmt.Sprintf("cannot write cache: %v", err)}
	}
	if onLog != nil {
		onLog("  - profile.yaml")
	}
	for _, fname := range profileFileCandidates {
		if fname == "profile.yaml" {
			continue
		}
		url := fmt.Sprintf("%s/%s@%s/hub/%s/%s", jsdelivrRawBase, HubRepo, HubRef(), name, fname)
		text, herr := httpGetTextOptional(url)
		if herr != nil {
			return "", herr
		}
		if text == "" {
			continue // optional file not present for this profile
		}
		if err := os.WriteFile(filepath.Join(cachePath, fname), []byte(text), 0o644); err != nil {
			return "", &HubError{Message: fmt.Sprintf("cannot write cache: %v", err)}
		}
		if onLog != nil {
			onLog("  - " + fname)
		}
	}
	writeCacheMeta(name)
	if herr := ensureSharedCached(refresh, onLog); herr != nil {
		return "", herr
	}
	return cachePath, nil
}

// ListProfiles lists the hub's profiles: the disk-cached tree for names,
// then each profile.yaml for metadata.
func ListProfiles(refresh bool, onLog func(string)) ([]HubProfile, *HubError) {
	if refresh {
		clearTreeCache()
	}
	names, herr := listProfileNames()
	if herr != nil {
		return nil, herr
	}
	var profiles []HubProfile
	for _, name := range names {
		url := fmt.Sprintf("%s/%s@%s/hub/%s/profile.yaml", jsdelivrRawBase, HubRepo, HubRef(), name)
		text, herr := httpGetText(url)
		if herr != nil {
			if onLog != nil {
				onLog(fmt.Sprintf("warning: could not read metadata for %s: %s", name, herr.Message))
			}
			profiles = append(profiles, HubProfile{Name: name, Description: "(failed to read metadata)", Tested: "unverified"})
			continue
		}
		profiles = append(profiles, hubProfileFromYAML(name, text))
	}
	return profiles, nil
}

// hubProfileFromYAML parses the metadata fields of a hub profile.yaml.
func hubProfileFromYAML(fallbackName, text string) HubProfile {
	var doc map[string]any
	if err := yaml.Unmarshal([]byte(text), &doc); err != nil {
		return HubProfile{Name: fallbackName, Description: "(failed to read metadata)", Tested: "unverified"}
	}
	p := HubProfile{
		Name:        fallbackName,
		Description: toString(doc["description"]),
		CLI:         toString(doc["cli"]),
		Tested:      "unverified",
	}
	_ = doc
	if s, ok := doc["name"].(string); ok && s != "" {
		p.Name = s
	}
	if s, ok := doc["tested"].(string); ok && s != "" {
		p.Tested = s
	}
	return p
}

// ShowReadme returns the README.md content for a profile (fetches if needed).
func ShowReadme(name string, refresh bool, onLog func(string)) (string, *HubError) {
	dir, herr := FetchProfile(name, refresh, onLog)
	if herr != nil {
		return "", herr
	}
	raw, err := os.ReadFile(filepath.Join(dir, "README.md"))
	if err != nil {
		return fmt.Sprintf("(no README.md for profile '%s')", name), nil
	}
	return string(raw), nil
}

// InstallProfile copies a cached profile into targetDir/<name>/, along with
// `_shared/` (the bridge scripts import it via a sibling path).
func InstallProfile(name string, targetDir string, refresh bool, onLog func(string)) (string, *HubError) {
	src, herr := FetchProfile(name, refresh, onLog)
	if herr != nil {
		return "", herr
	}
	dest := filepath.Join(targetDir, name)
	if _, err := os.Stat(dest); err == nil {
		return "", &HubError{Message: fmt.Sprintf("target already exists: %s", dest)}
	}
	if herr := copyDir(src, dest); herr != nil {
		return "", herr
	}
	_ = os.Remove(filepath.Join(dest, ".cache-meta.json"))
	if _, err := os.Stat(HubCacheDir("_shared")); err == nil {
		if _, err := os.Stat(filepath.Join(targetDir, "_shared")); err != nil {
			if herr := copyDir(HubCacheDir("_shared"), filepath.Join(targetDir, "_shared")); herr != nil {
				return "", herr
			}
		}
	}
	if onLog != nil {
		onLog("installed to: " + dest)
	}
	return dest, nil
}

func copyDir(src, dest string) *HubError {
	entries, err := os.ReadDir(src)
	if err != nil {
		return &HubError{Message: fmt.Sprintf("cannot read %s: %v", src, err)}
	}
	if err := os.MkdirAll(dest, 0o755); err != nil {
		return &HubError{Message: fmt.Sprintf("cannot create %s: %v", dest, err)}
	}
	for _, e := range entries {
		if e.Name() == "__pycache__" || strings.HasSuffix(e.Name(), ".pyc") {
			continue
		}
		s := filepath.Join(src, e.Name())
		d := filepath.Join(dest, e.Name())
		if e.IsDir() {
			if herr := copyDir(s, d); herr != nil {
				return herr
			}
			continue
		}
		raw, err := os.ReadFile(s)
		if err != nil {
			return &HubError{Message: fmt.Sprintf("cannot read %s: %v", s, err)}
		}
		if err := os.WriteFile(d, raw, 0o644); err != nil {
			return &HubError{Message: fmt.Sprintf("cannot write %s: %v", d, err)}
		}
	}
	return nil
}
