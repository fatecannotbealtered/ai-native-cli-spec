// Vendored from ai-native-cli-spec template/go/aicli. Do not edit in tool repos; run scripts/sync-spec.js.
// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Sean Guo

package aicli

import (
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"net"
	"net/http"
	"strings"
	"time"
)

// The network behaviour of CLI-110, exported so a tool's upstream client and aicli's
// own requests (update, doctor's refresh) share one implementation (P10).

const (
	// dialTimeout bounds connecting to a host; tlsHandshakeTimeout the TLS handshake.
	dialTimeout         = 10 * time.Second
	tlsHandshakeTimeout = 10 * time.Second
)

// HTTPTransport returns a transport that follows CLI-110: the proxy from
// HTTPS_PROXY, HTTP_PROXY and NO_PROXY; TLS 1.2 or newer, always verified, against
// the system roots plus the PEM file <PREFIX>_CA_FILE names; at most 10 s to connect
// and 10 s for the TLS handshake. An unusable CA file is E_CONFIG. Build the
// upstream client on it with a per-request timeout (at most 30 s) and UserAgent, and
// never re-send a write (CLI-110).
func (a *App) HTTPTransport() (*http.Transport, *Error) {
	tr := http.DefaultTransport.(*http.Transport).Clone()
	tr.Proxy = http.ProxyFromEnvironment
	tr.DialContext = (&net.Dialer{Timeout: dialTimeout, KeepAlive: 30 * time.Second}).DialContext
	tr.TLSHandshakeTimeout = tlsHandshakeTimeout
	tr.TLSClientConfig = &tls.Config{MinVersion: tls.VersionTLS12}
	prefix := a.envPrefix()
	if path := strings.TrimSpace(a.env(prefix + "_CA_FILE")); path != "" {
		pool, err := x509.SystemCertPool()
		if err != nil || pool == nil {
			pool = x509.NewCertPool()
		}
		pem, err := readSmallFile(path, 1<<20)
		if err != nil || !pool.AppendCertsFromPEM(pem) {
			return nil, NewError("E_CONFIG", fmt.Sprintf("%s_CA_FILE names %s, which is not a readable PEM certificate file.", prefix, path),
				map[string]any{"dispatch_state": "not_started",
					"next_action": fmt.Sprintf("Point %s_CA_FILE at a PEM file of CA certificates, or unset it.", prefix)})
		}
		tr.TLSClientConfig.RootCAs = pool
	}
	return tr, nil
}

// UserAgent is the User-Agent of every request (CLI-110): <tool>/<version>
// aicli/<spec version>.
func (a *App) UserAgent() string {
	return fmt.Sprintf("%s/%s aicli/%s", a.toolName(), a.version(), SpecVersion)
}
