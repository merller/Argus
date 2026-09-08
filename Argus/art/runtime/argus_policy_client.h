/*
 * Copyright (C) 2026 The Android Open Source Project
 * SPDX-License-Identifier: Apache-2.0
 */
#ifndef ART_RUNTIME_ARGUS_POLICY_CLIENT_H_
#define ART_RUNTIME_ARGUS_POLICY_CLIENT_H_

#include <cstdint>
#include <string_view>

namespace art::argus {

enum class KernelPolicyState {
  kApplied,
  kUnavailable,
  kError,
};

inline constexpr uint32_t kPolicyMatchDefault = 0;
inline constexpr uint32_t kPolicyMatchExact = 1;
inline constexpr uint32_t kPolicyMatchAnyAgent = 2;

struct KernelPolicyResult {
  KernelPolicyState state = KernelPolicyState::kError;
  int verdict = 2;  // Restricted is the fail-closed framework behavior.
  uint32_t match = 0;
  uint64_t generation = 0;
  uint64_t restricted_token = 0;
  int error_number = 0;
};

/* Returns ARGUS_OPERATION_UNSPECIFIED for an operation not in the UAPI. */
uint32_t PolicyOperationFromName(std::string_view operation);

/*
 * Queries the kernel for the calling process' UID.  dciplus_hex must be one
 * lowercase SHA-256 value.  The caller must only pass a subject that has
 * already been authenticated by Argus' trusted provenance verifier.
 */
KernelPolicyResult LookupKernelPolicy(std::string_view dciplus_hex,
                                      uint32_t operation,
                                      uint32_t verified_subject);

}  // namespace art::argus

/* Stable bridge used by libandroid_runtime after ART finalized the exact DCI+. */
extern "C" int ArtArgusLookupKernelPolicy(const char* dciplus_hex,
                                          const char* operation,
                                          uint32_t verified_subject,
                                          uint64_t* restricted_token);

#endif  // ART_RUNTIME_ARGUS_POLICY_CLIENT_H_
