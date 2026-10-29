// Vendored from ai-native-cli-spec template/go/aicli. Do not edit in tool repos; run scripts/sync-spec.js.
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Sean Guo

package aicli

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
)

// The contract corpus (template/common/contract/examples) is the shared truth for
// both libraries and check-contract.py: every valid example must pass the library's
// validators and every invalid one must fail them (decision 15). This test runs in
// the spec repository only; a tool repo vendors the library without the corpus.

type corpusEntry struct {
	File      string `json:"file"`
	Schema    string `json:"schema"`
	Expect    string `json:"expect"`
	Rule      string `json:"rule"`
	Violation struct {
		Check any `json:"check"` // a check name, or a list of them
	} `json:"violation"`
}

// budgetProblems is the success budget of a default invocation, which conformance
// R04 checks and ValidateEnvelope does not (Q24): the corpus's success_budget
// examples are checked with it.
func budgetProblems(e corpusEntry, doc any) []string {
	env, ok := doc.(map[string]any)
	if !ok || e.Violation.Check != "success_budget" {
		return nil
	}
	if size, over := overSuccessBudget(env); over {
		return []string{"the response is " + itoa(size) + " characters, over the success budget (R04)"}
	}
	return nil
}

// corpusValidators maps a corpus schema name to the library check for it.
var corpusValidators = map[string]func(doc any) []string{
	"envelope": func(doc any) []string {
		v := &validation{}
		validateEnvelopeDoc(v, doc, false)
		return v.problems
	},
	"envelope#/$defs/stream_line": func(doc any) []string {
		v := &validation{}
		validateEnvelopeDoc(v, doc, true)
		return v.problems
	},
	"error-details": func(doc any) []string {
		v := &validation{}
		genericChecks(v, doc, "", false)
		validateErrorObject(v, "error", doc)
		return v.problems
	},
	"preview": func(doc any) []string {
		v := &validation{}
		genericChecks(v, doc, "", false)
		validatePreview(v, "preview", doc)
		return v.problems
	},
	"update": func(doc any) []string {
		v := &validation{}
		genericChecks(v, doc, "", false)
		validateUpdateData(v, "update", doc)
		return v.problems
	},
	"receipt": func(doc any) []string {
		v := &validation{}
		genericChecks(v, doc, "", false)
		validateReceipt(v, "receipt", doc)
		return v.problems
	},
	"batch": func(doc any) []string {
		v := &validation{}
		genericChecks(v, doc, "", false)
		if exactKeys(v, "batch", doc, []string{"items", "summary"}, []string{"operation_id", "omitted"}) {
			b := doc.(map[string]any)
			if id, has := b["operation_id"]; has && !matches(batchIDRE, id) {
				v.add("batch.operation_id", "must be a batch id")
			}
			validateBatch(v, "batch", b)
		}
		return v.problems
	},
	"page": func(doc any) []string {
		v := &validation{}
		genericChecks(v, doc, "", false)
		if m, ok := doc.(map[string]any); ok {
			validatePage(v, "page", m)
		} else {
			v.add("page", "must be an object")
		}
		return v.problems
	},
	"operation": func(doc any) []string {
		v := &validation{}
		genericChecks(v, doc, "", false)
		validateOperation(v, "operation", doc)
		return v.problems
	},
	"notice": func(doc any) []string {
		v := &validation{}
		genericChecks(v, doc, "", false)
		validateNotice(v, "notice", doc)
		return v.problems
	},
	"scope": func(doc any) []string {
		v := &validation{}
		genericChecks(v, doc, "", false)
		validateScopeObject(v, "scope", doc)
		return v.problems
	},
	"context": func(doc any) []string { return dataCheck(doc, validateContext) },
	"doctor":  func(doc any) []string { return dataCheck(doc, validateDoctor) },
	"changelog": func(doc any) []string {
		return dataCheck(doc, validateChangelog)
	},
	"reference": func(doc any) []string { return dataCheck(doc, validateReferenceFull) },
	"reference-index": func(doc any) []string {
		out := dataCheck(doc, validateReferenceIndex)
		if compactSize(doc) > indexBudgetChars {
			out = append(out, "the index is over its budget")
		}
		return out
	},
	"reference-command": func(doc any) []string {
		out := dataCheck(doc, validateReferenceCommand)
		if compactSize(doc) > commandBudgetChars {
			out = append(out, "the command entry is over its budget")
		}
		return out
	},
}

func dataCheck(doc any, check func(map[string]any) error) []string {
	v := &validation{}
	genericChecks(v, doc, "", false)
	m, ok := doc.(map[string]any)
	if !ok {
		return append(v.problems, "must be an object")
	}
	if err := check(m); err != nil {
		v.problems = append(v.problems, err.Error())
	}
	return v.problems
}

func TestContractCorpus(t *testing.T) {
	dir := filepath.Join("..", "..", "common", "contract", "examples")
	raw, err := os.ReadFile(filepath.Join(dir, "index.json"))
	if err != nil {
		t.Skipf("the contract corpus is not available here: %v", err)
	}
	var index struct {
		Examples []corpusEntry `json:"examples"`
	}
	if err := json.Unmarshal(raw, &index); err != nil {
		t.Fatal(err)
	}
	checked := map[string]int{}
	skipped := map[string]int{}
	for _, e := range index.Examples {
		check, ok := corpusValidators[e.Schema]
		if !ok {
			skipped[e.Schema]++
			continue
		}
		b, err := os.ReadFile(filepath.Join(dir, filepath.FromSlash(e.File)))
		if err != nil {
			t.Errorf("%s: %v", e.File, err)
			continue
		}
		pv := &validation{}
		doc, parsed := parseDocument(pv, b)
		problems := append([]string{}, pv.problems...)
		if parsed {
			problems = append(problems, check(doc)...)
			problems = append(problems, budgetProblems(e, doc)...)
		}
		checked[e.Schema]++
		switch {
		case e.Expect == "valid" && len(problems) > 0:
			t.Errorf("%s (%s): valid example rejected: %s", e.File, e.Rule, strings.Join(problems, "; "))
		case e.Expect == "invalid" && len(problems) == 0:
			t.Errorf("%s (%s): invalid example accepted", e.File, e.Rule)
		}
	}
	var names []string
	for k, n := range checked {
		names = append(names, k+"="+itoa(n))
	}
	sort.Strings(names)
	t.Logf("checked %s; not covered by a library validator: %v", strings.Join(names, " "), skipped)
	if checked["envelope"] == 0 {
		t.Fatal("no envelope example was checked")
	}
}

// TestValidateEnvelopeOnTheCorpusBytes runs the exported ValidateEnvelope on every
// envelope example as compact bytes, the form a command writes.
func TestValidateEnvelopeOnTheCorpusBytes(t *testing.T) {
	dir := filepath.Join("..", "..", "common", "contract", "examples")
	raw, err := os.ReadFile(filepath.Join(dir, "index.json"))
	if err != nil {
		t.Skipf("the contract corpus is not available here: %v", err)
	}
	var index struct {
		Examples []corpusEntry `json:"examples"`
	}
	if err := json.Unmarshal(raw, &index); err != nil {
		t.Fatal(err)
	}
	for _, e := range index.Examples {
		if e.Schema != "envelope" {
			continue
		}
		b, err := os.ReadFile(filepath.Join(dir, filepath.FromSlash(e.File)))
		if err != nil {
			t.Fatal(err)
		}
		var compact bytes.Buffer
		if err := json.Compact(&compact, b); err != nil {
			if e.Expect == "valid" {
				t.Errorf("%s: %v", e.File, err)
			}
			continue
		}
		compact.WriteByte('\n')
		err = ValidateEnvelope(compact.Bytes())
		if err == nil && e.Violation.Check == "success_budget" {
			// ValidateEnvelope leaves the success budget to conformance R04 (Q24).
			var doc any
			if json.Unmarshal(compact.Bytes(), &doc) == nil && len(budgetProblems(e, doc)) == 0 {
				t.Errorf("%s: the R04 budget check accepts it", e.File)
			}
			continue
		}
		switch {
		case e.Expect == "valid" && err != nil:
			t.Errorf("%s: valid example rejected: %v", e.File, err)
		case e.Expect == "invalid" && err == nil:
			t.Errorf("%s: invalid example accepted", e.File)
		}
	}
}

func itoa(n int) string {
	b, _ := json.Marshal(n)
	return string(b)
}
