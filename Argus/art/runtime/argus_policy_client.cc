/*
 * Copyright (C) 2026 The Android Open Source Project
 * SPDX-License-Identifier: Apache-2.0
 */

#include "argus_policy_client.h"

#include <errno.h>
#include <fcntl.h>
#include <linux/argus_policy.h>
#include <sys/ioctl.h>
#include <unistd.h>

#include <array>
#include <atomic>
#include <cstddef>
#include <cstdint>
#include <mutex>

namespace art::argus {
namespace {

constexpr char kPolicyDevice[] = "/dev/argus_policy";
constexpr int kFdUnknown = -1;
constexpr int kFdUnavailable = -2;

std::atomic<int> g_policy_fd{kFdUnknown};
std::mutex g_policy_fd_lock;

int HexNibble(char value) {
  if (value >= '0' && value <= '9') {
    return value - '0';
  }
  if (value >= 'a' && value <= 'f') {
    return value - 'a' + 10;
  }
  return -1;
}

bool DecodeDciPlus(std::string_view encoded,
                   std::array<uint8_t, ARGUS_POLICY_DIGEST_SIZE>* decoded) {
  if (encoded.size() != decoded->size() * 2u) {
    return false;
  }
  for (size_t i = 0; i < decoded->size(); ++i) {
    const int high = HexNibble(encoded[i * 2u]);
    const int low = HexNibble(encoded[i * 2u + 1u]);
    if (high < 0 || low < 0) {
      return false;
    }
    (*decoded)[i] = static_cast<uint8_t>((high << 4) | low);
  }
  return true;
}

bool ValidOperation(uint32_t operation) {
  return operation > ARGUS_OPERATION_UNSPECIFIED &&
         operation < ARGUS_OPERATION_MAX;
}

bool ValidSubject(uint32_t subject) {
  return subject == ARGUS_SUBJECT_AGENT;
}

bool ValidVerdict(uint32_t verdict) {
  return verdict == ARGUS_VERDICT_ALLOW || verdict == ARGUS_VERDICT_DENY ||
         verdict == ARGUS_VERDICT_RESTRICTED;
}

int GetPolicyFd(int* error_number) {
  int fd = g_policy_fd.load(std::memory_order_acquire);
  if (fd >= 0 || fd == kFdUnavailable) {
    if (fd == kFdUnavailable) {
      *error_number = ENOENT;
    }
    return fd;
  }

  std::lock_guard<std::mutex> lock(g_policy_fd_lock);
  fd = g_policy_fd.load(std::memory_order_relaxed);
  if (fd != kFdUnknown) {
    if (fd == kFdUnavailable) {
      *error_number = ENOENT;
    }
    return fd;
  }

  do {
    fd = open(kPolicyDevice, O_RDONLY | O_CLOEXEC);
  } while (fd < 0 && errno == EINTR);
  if (fd < 0) {
    *error_number = errno;
    /* The device is created during boot; avoid an open() on every click. */
    if (errno == ENOENT || errno == ENODEV) {
      g_policy_fd.store(kFdUnavailable, std::memory_order_release);
      return kFdUnavailable;
    }
    return kFdUnknown;
  }
  g_policy_fd.store(fd, std::memory_order_release);
  return fd;
}

}  // namespace

uint32_t PolicyOperationFromName(std::string_view operation) {
  if (operation == "click") {
    return ARGUS_OPERATION_CLICK;
  }
  if (operation == "long_click") {
    return ARGUS_OPERATION_LONG_CLICK;
  }
  if (operation == "text_commit") {
    return ARGUS_OPERATION_TEXT_COMMIT;
  }
  if (operation == "scroll") {
    return ARGUS_OPERATION_SCROLL;
  }
  if (operation == "gesture") {
    return ARGUS_OPERATION_GESTURE;
  }
  return ARGUS_OPERATION_UNSPECIFIED;
}

KernelPolicyResult LookupKernelPolicy(std::string_view dciplus_hex,
                                      uint32_t operation,
                                      uint32_t verified_subject) {
  KernelPolicyResult result;
  std::array<uint8_t, ARGUS_POLICY_DIGEST_SIZE> dciplus{};
  if (!DecodeDciPlus(dciplus_hex, &dciplus) || !ValidOperation(operation) ||
      !ValidSubject(verified_subject)) {
    result.error_number = EINVAL;
    return result;
  }

  int open_error = 0;
  const int fd = GetPolicyFd(&open_error);
  if (fd == kFdUnavailable) {
    result.state = KernelPolicyState::kUnavailable;
    result.error_number = open_error;
    return result;
  }
  if (fd < 0) {
    result.error_number = open_error != 0 ? open_error : EIO;
    return result;
  }

  argus_policy_query query{};
  query.abi_version = ARGUS_POLICY_ABI_VERSION;
  query.key.operation = operation;
  query.key.subject = verified_subject;
  for (size_t i = 0; i < dciplus.size(); ++i) {
    query.key.dciplus[i] = dciplus[i];
  }

  int rc;
  do {
    rc = ioctl(fd, ARGUS_POLICY_IOC_QUERY, &query);
  } while (rc < 0 && errno == EINTR);
  if (rc < 0) {
    result.error_number = errno;
    return result;
  }
  if (query.abi_version != ARGUS_POLICY_ABI_VERSION ||
      !ValidVerdict(query.verdict) || query.match > ARGUS_MATCH_EXACT) {
    result.error_number = EPROTO;
    return result;
  }

  result.state = KernelPolicyState::kApplied;
  result.verdict = static_cast<int>(query.verdict);
  result.match = query.match;
  result.generation = query.generation;
  result.restricted_token = query.restricted_token;
  result.error_number = 0;
  return result;
}

}  // namespace art::argus

extern "C" int ArtArgusLookupKernelPolicy(const char* dciplus_hex,
                                          const char* operation,
                                          uint32_t verified_subject,
                                          uint64_t* restricted_token) {
  if (restricted_token != nullptr) *restricted_token = 0;
  if (dciplus_hex == nullptr || operation == nullptr) {
    return 2;  // Restricted: a malformed security context fails closed.
  }
  const auto result = art::argus::LookupKernelPolicy(
      dciplus_hex, art::argus::PolicyOperationFromName(operation), verified_subject);
  if (restricted_token != nullptr) *restricted_token = result.restricted_token;
  return result.state == art::argus::KernelPolicyState::kApplied ? result.verdict : 2;
}
