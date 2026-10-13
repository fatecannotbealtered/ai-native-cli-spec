package main

import (
	"encoding/json"
	"errors"
	"os"
	"sort"
	"strconv"
	"sync"
	"time"
)

// The fake upstream: a tiny notes service with HTTP-like errors. Tests use it in
// memory and read Writes() to prove the write gate sent nothing; the binary keeps it
// in a JSON file so a dry-run and its confirm see the same notes.

// Note is one note as the service returns it.
type Note struct {
	ID        string   `json:"id"`
	Ref       string   `json:"ref"`
	Title     string   `json:"title"`
	Body      string   `json:"body"`
	Tags      []string `json:"tags"`
	Version   int      `json:"version"`
	UpdatedAt string   `json:"updated_at"`
}

// APIError is the service's error: an HTTP status and the service's own message.
// Status 0 means the request never left the client.
type APIError struct {
	Status  int
	Message string
}

func (e *APIError) Error() string { return strconv.Itoa(e.Status) + " " + e.Message }

// Service is what the CLI needs from the notes service.
type Service interface {
	List(tag string) ([]Note, error)
	Get(id string) (Note, error)
	Create(title, body string, tags []string, idempotencyKey string) (Note, error)
	Update(id string, title, body *string, addTags, removeTags []string) (Note, error)
	Delete(id string) error
	Share(id, to, token string) (time.Time, error)
	AddTag(id, tag string) (Note, error)
}

// memStore is an in-memory Service that counts the write requests it receives.
type memStore struct {
	mu     sync.Mutex
	notes  map[string]*Note
	next   int
	writes int
	keys   map[string]string // idempotency key -> note id
	now    func() time.Time
	// fail, when set, decides the outcome of a write before it is applied (tests).
	fail func(op, id string) error
	save func(*memStore)
}

func newMemStore() *memStore {
	s := &memStore{now: func() time.Time { return time.Date(2026, 10, 12, 9, 0, 0, 0, time.UTC) }}
	s.Reset()
	return s
}

// Reset restores the seed notes.
func (s *memStore) Reset() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.notes = map[string]*Note{}
	for i, n := range []struct{ title, body, tag string }{
		{"Groceries", "Milk, eggs, coffee.", "home"},
		{"Release plan", "Cut 2.0.0 on Friday.", "work"},
		{"Reading list", "The Design of Everyday Things.", "home"},
	} {
		id := strconv.Itoa(i + 1)
		s.notes[id] = &Note{ID: id, Ref: "notes/" + id, Title: n.title, Body: n.body, Tags: []string{n.tag}, Version: 1,
			UpdatedAt: "2026-10-01T08:00:00Z"}
	}
	s.next = 4
	s.writes = 0
	s.keys = map[string]string{}
}

// Writes is the number of write requests that reached the service.
func (s *memStore) Writes() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.writes
}

func copyNote(n *Note) Note {
	c := *n
	c.Tags = append([]string{}, n.Tags...)
	return c
}

func (s *memStore) List(tag string) ([]Note, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	var out []Note
	for _, n := range s.notes {
		if tag == "" || hasTag(n.Tags, tag) {
			out = append(out, copyNote(n))
		}
	}
	sort.Slice(out, func(i, j int) bool {
		a, _ := strconv.Atoi(out[i].ID)
		b, _ := strconv.Atoi(out[j].ID)
		return a < b
	})
	return out, nil
}

func (s *memStore) Get(id string) (Note, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	n, ok := s.notes[id]
	if !ok {
		return Note{}, &APIError{Status: 404, Message: "note not found"}
	}
	return copyNote(n), nil
}

// write runs one write request: the failure hook first (it may claim the request
// never left, or was refused), then the change, then persistence.
func (s *memStore) write(op, id string, fn func() error) error {
	s.mu.Lock()
	fail := s.fail
	s.mu.Unlock()
	if fail != nil {
		if err := fail(op, id); err != nil {
			var api *APIError
			if errors.As(err, &api) && api.Status == 0 {
				return err // never sent
			}
			s.mu.Lock()
			s.writes++
			s.mu.Unlock()
			return err
		}
	}
	s.mu.Lock()
	s.writes++
	err := fn()
	save := s.save
	s.mu.Unlock()
	if save != nil && err == nil {
		save(s)
	}
	return err
}

func (s *memStore) Create(title, body string, tags []string, key string) (Note, error) {
	var out Note
	err := s.write("create", "", func() error {
		if key != "" {
			if id, ok := s.keys[key]; ok {
				out = copyNote(s.notes[id])
				return nil
			}
		}
		id := strconv.Itoa(s.next)
		s.next++
		n := &Note{ID: id, Ref: "notes/" + id, Title: title, Body: body, Tags: append([]string{}, tags...), Version: 1,
			UpdatedAt: s.now().Format(time.RFC3339)}
		s.notes[id] = n
		if key != "" {
			s.keys[key] = id
		}
		out = copyNote(n)
		return nil
	})
	return out, err
}

func (s *memStore) Update(id string, title, body *string, addTags, removeTags []string) (Note, error) {
	var out Note
	err := s.write("update", id, func() error {
		n, ok := s.notes[id]
		if !ok {
			return &APIError{Status: 404, Message: "note not found"}
		}
		if title != nil {
			n.Title = *title
		}
		if body != nil {
			n.Body = *body
		}
		for _, t := range addTags {
			if !hasTag(n.Tags, t) {
				n.Tags = append(n.Tags, t)
			}
		}
		var kept []string
		for _, t := range n.Tags {
			if !hasTag(removeTags, t) {
				kept = append(kept, t)
			}
		}
		n.Tags = kept
		n.Version++
		n.UpdatedAt = s.now().Format(time.RFC3339)
		out = copyNote(n)
		return nil
	})
	return out, err
}

func (s *memStore) Delete(id string) error {
	return s.write("delete", id, func() error {
		if _, ok := s.notes[id]; !ok {
			return &APIError{Status: 404, Message: "note not found"}
		}
		delete(s.notes, id)
		return nil
	})
}

func (s *memStore) Share(id, to, token string) (time.Time, error) {
	at := s.now()
	err := s.write("share", id, func() error {
		if _, ok := s.notes[id]; !ok {
			return &APIError{Status: 404, Message: "note not found"}
		}
		if token == "revoked-token" {
			return &APIError{Status: 403, Message: "sharing token revoked"}
		}
		return nil
	})
	return at, err
}

func (s *memStore) AddTag(id, tag string) (Note, error) {
	return s.Update(id, nil, nil, []string{tag}, nil)
}

func hasTag(tags []string, t string) bool {
	for _, x := range tags {
		if x == t {
			return true
		}
	}
	return false
}

// fileStore persists a memStore in a JSON file between runs of the binary.
func fileStore(path string) *memStore {
	s := newMemStore()
	if b, err := os.ReadFile(path); err == nil {
		var saved struct {
			Notes map[string]*Note `json:"notes"`
			Next  int              `json:"next"`
			Keys  map[string]string
		}
		if json.Unmarshal(b, &saved) == nil && saved.Notes != nil {
			s.notes, s.next = saved.Notes, saved.Next
			if saved.Keys != nil {
				s.keys = saved.Keys
			}
		}
	}
	s.save = func(m *memStore) {
		m.mu.Lock()
		b, err := json.MarshalIndent(map[string]any{"notes": m.notes, "next": m.next, "Keys": m.keys}, "", "  ")
		m.mu.Unlock()
		if err == nil {
			_ = os.WriteFile(path, b, 0o600)
		}
	}
	return s
}
