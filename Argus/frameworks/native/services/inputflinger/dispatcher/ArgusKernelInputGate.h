/*
 * Copyright (C) 2026 The Android Open Source Project
 * SPDX-License-Identifier: Apache-2.0
 */
#pragma once

#include <input/ArgusSubjectTag.h>

namespace android::inputdispatcher {

/*
 * Consults the kernel-owned global Restricted state.  Physical input never
 * reaches this function; malformed subjects and backend failures fail closed.
 */
bool ArgusKernelAllowsAgentInput(ArgusSubjectSource source);

}  // namespace android::inputdispatcher
