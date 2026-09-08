/*
 * Copyright (C) 2026 The Android Open Source Project
 * SPDX-License-Identifier: Apache-2.0
 */

#define LOG_TAG "ArgusConfirmationService"

#include <android-base/logging.h>
#include <fcntl.h>
#include <linux/argus_policy.h>
#include <nativehelper/JNIHelp.h>
#include <sys/ioctl.h>
#include <unistd.h>

#include <cerrno>

#include "core_jni_helpers.h"

namespace android {
namespace {

jboolean nativeResolveRestricted(JNIEnv*, jclass, jlong restrictedToken,
                                  jint targetUid, jboolean approved) {
    if (restrictedToken == 0 || targetUid < 0) return JNI_FALSE;

    int fd;
    do {
        fd = open("/dev/argus_policy", O_RDONLY | O_CLOEXEC);
    } while (fd < 0 && errno == EINTR);
    if (fd < 0) {
        PLOG(ERROR) << "Cannot open the Argus policy device to resolve Restricted state";
        return JNI_FALSE;
    }

    argus_policy_restricted_resolution resolution{};
    resolution.abi_version = ARGUS_POLICY_ABI_VERSION;
    resolution.target_uid = static_cast<uint32_t>(targetUid);
    resolution.restricted_token = static_cast<uint64_t>(restrictedToken);
    resolution.approved = approved == JNI_TRUE ? 1 : 0;

    int result;
    do {
        result = ioctl(fd, ARGUS_POLICY_IOC_RESOLVE_RESTRICTED, &resolution);
    } while (result < 0 && errno == EINTR);
    const int savedErrno = errno;
    close(fd);
    if (result < 0) {
        errno = savedErrno;
        PLOG(ERROR) << "Cannot resolve the Argus Restricted state";
        return JNI_FALSE;
    }
    return JNI_TRUE;
}

const JNINativeMethod gMethods[] = {
        {"nativeResolveRestricted", "(JIZ)Z",
         reinterpret_cast<void*>(nativeResolveRestricted)},
};

}  // namespace

int register_com_android_server_input_ArgusConfirmationService(JNIEnv* env) {
    RegisterMethodsOrDie(env, "com/android/server/input/ArgusConfirmationService",
                         gMethods, NELEM(gMethods));
    return 0;
}

}  // namespace android
