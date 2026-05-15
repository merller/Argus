/*
 * Core ARGUS addition for
 * frameworks/base/core/jni/android_view_ArgusRestrictedPolicy.cpp.
 */

namespace {

static constexpr int kVerdictAllow = 0;
static constexpr int kVerdictBlock = 1;
static constexpr int kVerdictRestricted = 2;

struct ArgusVerdictKey {
    std::string domain;
    std::string codebase;
    jint index;
    jint agent_source;
};

static int LookupRestrictedVerdict(const ArgusVerdictKey& key) {
    if (key.domain.empty() || key.codebase.empty() || key.index < 0) {
        return kVerdictAllow;
    }

    /*
     * Production ARGUS reads the policy table populated by the monitor from the
     * kernel-backed DCI decision channel. The GitHub artifact keeps this as the
     * single replacement point so the framework protocol remains independent of
     * the policy storage backend used in an experiment.
     */
    return ArgusPolicyTable::GetInstance().Lookup(
            key.domain.c_str(),
            key.codebase.c_str(),
            key.index,
            key.agent_source,
            kVerdictAllow);
}

static std::string JStringToStdString(JNIEnv* env, jstring value) {
    if (value == nullptr) {
        return std::string();
    }
    const char* chars = env->GetStringUTFChars(value, nullptr);
    if (chars == nullptr) {
        return std::string();
    }
    std::string out(chars);
    env->ReleaseStringUTFChars(value, chars);
    return out;
}

static jint nativeGetVerdict(JNIEnv* env, jclass, jstring domain, jstring codebase,
        jint index, jint agent_source) {
    ArgusVerdictKey key = {
            .domain = JStringToStdString(env, domain),
            .codebase = JStringToStdString(env, codebase),
            .index = index,
            .agent_source = agent_source,
    };
    return LookupRestrictedVerdict(key);
}

static const JNINativeMethod gArgusRestrictedPolicyMethods[] = {
    { "nativeGetVerdict", "(Ljava/lang/String;Ljava/lang/String;II)I",
            reinterpret_cast<void*>(nativeGetVerdict) },
};

}  // namespace
