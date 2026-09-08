/*
 * Copyright (C) 2026 The Android Open Source Project
 * SPDX-License-Identifier: Apache-2.0
 */

#define LOG_TAG "ArgusKernelInputGate"

#include "ArgusKernelInputGate.h"

#include <android-base/logging.h>
#include <fcntl.h>
#include <linux/argus_policy.h>
#include <sys/ioctl.h>
#include <unistd.h>

#include <cerrno>
#include <mutex>

namespace android::inputdispatcher {
namespace {

constexpr char kPolicyDevice[] = "/dev/argus_policy";
std::mutex gFdLock;
int gPolicyFd = -1;

bool isAgent(ArgusSubjectSource source) {
    return source == ArgusSubjectSource::AGENT;
}

int getPolicyFd() {
    std::lock_guard<std::mutex> lock(gFdLock);
    if (gPolicyFd >= 0) return gPolicyFd;

    int fd;
    do {
        fd = open(kPolicyDevice, O_RDONLY | O_CLOEXEC);
    } while (fd < 0 && errno == EINTR);
    if (fd >= 0) gPolicyFd = fd;
    return fd;
}

}  // namespace

bool ArgusKernelAllowsAgentInput(ArgusSubjectSource source) {
    if (!isAgent(source)) return false;

    const int fd = getPolicyFd();
    if (fd < 0) {
        PLOG(ERROR) << "Cannot query the Argus input freeze; dropping agent input";
        return false;
    }

    argus_policy_input_check check{};
    check.abi_version = ARGUS_POLICY_ABI_VERSION;
    check.subject = static_cast<uint32_t>(source);
    int result;
    do {
        result = ioctl(fd, ARGUS_POLICY_IOC_INPUT_CHECK, &check);
    } while (result < 0 && errno == EINTR);
    if (result < 0) {
        PLOG(ERROR) << "Invalid Argus input-freeze response; dropping agent input";
        return false;
    }
    if (check.abi_version != ARGUS_POLICY_ABI_VERSION || check.reserved != 0 ||
        check.allowed > 1) {
        LOG(ERROR) << "Malformed Argus input-freeze response; dropping agent input";
        return false;
    }
    return check.allowed == 1;
}

}  // namespace android::inputdispatcher
