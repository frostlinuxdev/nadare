// SPDX-License-Identifier: GPL-3.0-or-later
//go:build !windows

package main

import (
	"fmt"
	"os"
)

// The stub only does something useful as a Windows program (it serves a Wine
// named pipe). This keeps `go vet` and `go test` working on Linux.
func main() {
	fmt.Fprintln(os.Stderr, "czn_pcsdk_stub: build with GOOS=windows")
	os.Exit(2)
}
