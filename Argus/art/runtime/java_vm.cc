#define LOG_TAG "ArgusDci"

#include <jni.h>
#include <openssl/sha.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <cctype>
#include <cinttypes>
#include <cstdint>
#include <cstring>
#include <mutex>
#include <string>
#include <unordered_map>
#include <vector>

#include <unistd.h>

#include "android-base/properties.h"
#include "android-base/stringprintf.h"
#include "argus_continuation.h"
#include "art_method-inl.h"
#include "base/logging.h"
#include "base/mutex.h"
#include "base/time_utils.h"
#include "class_linker.h"
#include "dex/code_item_accessors-inl.h"
#include "dex/dex_file.h"
#include "gc/scoped_gc_critical_section.h"
#include "instrumentation.h"
#include "jni/jni_internal.h"
#include "mirror/class-inl.h"
#include "scoped_thread_state_change-inl.h"
#include "runtime.h"
#include "thread-current-inl.h"
#include "thread_list.h"

namespace art {
namespace {

constexpr size_t kMaxMeasuredDexBytes = 64u * 1024u * 1024u;
constexpr size_t kMaxMeasuredCodeBytes = 1u * 1024u * 1024u;
constexpr size_t kMaxDexCacheEntries = 4096u;
constexpr size_t kMaxMethodCacheEntries = 16384u;

std::mutex g_hash_cache_lock;
std::unordered_map<std::string, std::string> g_dex_hash_cache;
std::unordered_map<std::string, std::string> g_method_hash_cache;

class ScopedUtfChars {
 public:
  ScopedUtfChars(JNIEnv* env, jstring value)
      : env_(env), value_(value), chars_(value != nullptr ? env->GetStringUTFChars(value, nullptr)
                                                        : nullptr) {}

  ~ScopedUtfChars() {
    if (chars_ != nullptr) {
      env_->ReleaseStringUTFChars(value_, chars_);
    }
  }

  const char* Get(const char* fallback) const { return chars_ != nullptr ? chars_ : fallback; }
  bool Valid() const { return value_ == nullptr || chars_ != nullptr; }

 private:
  JNIEnv* env_;
  jstring value_;
  const char* chars_;
};

std::string Hex(const uint8_t* bytes, size_t size) {
  static constexpr char kDigits[] = "0123456789abcdef";
  std::string result(size * 2u, '0');
  for (size_t index = 0; index < size; ++index) {
    result[index * 2u] = kDigits[bytes[index] >> 4u];
    result[index * 2u + 1u] = kDigits[bytes[index] & 0x0fu];
  }
  return result;
}

std::string Sha256(const uint8_t* bytes, size_t size) {
  std::array<uint8_t, SHA256_DIGEST_LENGTH> digest;
  SHA256(bytes, size, digest.data());
  return Hex(digest.data(), digest.size());
}

std::string Sha256(const std::string& value) {
  return Sha256(reinterpret_cast<const uint8_t*>(value.data()), value.size());
}

bool IsLowerHexSha256(const std::string& value);

std::string CertifiedDialogRecipe(const char* route) {
  static constexpr char kPrefix[] = "ARGUS/CERTIFIED-DIALOG/v1|";
  if (route == nullptr || std::strncmp(route, kPrefix, sizeof(kPrefix) - 1u) != 0) {
    return {};
  }
  const char* role_begin = route + sizeof(kPrefix) - 1u;
  const char* role_end = std::strchr(role_begin, '|');
  if (role_end == nullptr) {
    return {};
  }
  const std::string role(role_begin, role_end);
  if (role != "-1" && role != "-2" && role != "-3") {
    return {};
  }
  return std::string(kPrefix) + role;
}

std::string CertifiedSelfRouteRecipe(const char* route) {
  static constexpr char kPrefix[] = "ARGUS/CERTIFIED-SELF-ROUTE/v1|";
  if (route == nullptr || std::strncmp(route, kPrefix, sizeof(kPrefix) - 1u) != 0) {
    return {};
  }
  const std::string digest(route + sizeof(kPrefix) - 1u);
  if (!IsLowerHexSha256(digest)) {
    return {};
  }
  return std::string(kPrefix) + digest;
}

std::string Sha256DexSections(const uint8_t* begin,
                              size_t size,
                              const uint8_t* data_begin,
                              size_t data_size) {
  SHA256_CTX context;
  SHA256_Init(&context);
  const uint64_t primary_size = size;
  const uint64_t secondary_size = data_size;
  SHA256_Update(&context, &primary_size, sizeof(primary_size));
  SHA256_Update(&context, begin, size);
  if (data_begin != begin || data_size != size) {
    SHA256_Update(&context, &secondary_size, sizeof(secondary_size));
    SHA256_Update(&context, data_begin, data_size);
  }
  std::array<uint8_t, SHA256_DIGEST_LENGTH> digest;
  SHA256_Final(digest.data(), &context);
  return Hex(digest.data(), digest.size());
}

std::string SanitizeForLog(std::string value) {
  std::replace_if(value.begin(), value.end(), [](unsigned char character) {
    return std::isspace(character) || !std::isprint(character);
  }, '_');
  return value;
}

std::string CodeContainer(const std::string& dex_location) {
  const size_t separator = dex_location.find('!');
  return separator == std::string::npos
      ? dex_location : dex_location.substr(0u, separator);
}

bool IsApkCodeLocation(const std::string& dex_location) {
  const std::string container = CodeContainer(dex_location);
  return container.size() >= 4u &&
      container.compare(container.size() - 4u, 4u, ".apk") == 0;
}

std::string InstallSetKey(const std::string& dex_location) {
  const std::string container = CodeContainer(dex_location);
  if (container.compare(0u, std::strlen("/data/app/"), "/data/app/") == 0) {
    // base.apk and split APKs share one randomized installation directory.  This key is used
    // only for an in-process membership comparison and is never part of SelectorID.
    const size_t slash = container.rfind('/');
    return slash == std::string::npos ? std::string() : container.substr(0u, slash);
  }
  // A preinstalled application normally has one APK container.  Do not group it with
  // framework/core jars merely because they share a parent directory.
  return IsApkCodeLocation(container) ? container : std::string();
}

bool IsLowerHexSha256(const std::string& value) {
  if (value.size() != SHA256_DIGEST_LENGTH * 2u) {
    return false;
  }
  return std::all_of(value.begin(), value.end(), [](unsigned char character) {
    return std::isdigit(character) || (character >= 'a' && character <= 'f');
  });
}

const char* InputSourceName(jint source) {
  switch (source) {
    case 1:
      return "physical";
    case 2:
      return "injected";
    case 3:
      return "accessibility";
    case 4:
      return "ime";
    default:
      return "unknown";
  }
}

bool LookupHash(const std::string& key,
                std::unordered_map<std::string, std::string>* cache,
                std::string* hash) {
  std::lock_guard<std::mutex> lock(g_hash_cache_lock);
  const auto iterator = cache->find(key);
  if (iterator == cache->end()) {
    return false;
  }
  *hash = iterator->second;
  return true;
}

bool CacheHash(const std::string& key,
               const std::string& hash,
               size_t maximum_entries,
               std::unordered_map<std::string, std::string>* cache) {
  std::lock_guard<std::mutex> lock(g_hash_cache_lock);
  if (cache->find(key) != cache->end()) {
    return true;
  }
  if (cache->size() >= maximum_entries) {
    return false;
  }
  cache->emplace(key, hash);
  return true;
}

struct ResolvedCode {
  ArtMethod* method = nullptr;
  const uint8_t* dex_begin = nullptr;
  size_t dex_size = 0u;
  const uint8_t* data_begin = nullptr;
  size_t data_size = 0u;
  const uint8_t* code_begin = nullptr;
  size_t code_size = 0u;
  uint32_t dex_checksum = 0u;
  uint32_t method_index = dex::kDexNoIndex;
  uint64_t registration_index = 0u;
  std::string declaring_class;
  std::string method_name;
  std::string dex_location;
  bool native_method = false;
  bool abstract_method = false;
};

bool ResolveArtMethodCode(Thread* self, ArtMethod* method, ResolvedCode* resolved)
    REQUIRES_SHARED(Locks::mutator_lock_) {
  if (self == nullptr || method == nullptr || resolved == nullptr || method->IsProxyMethod()) {
    return false;
  }
  const DexFile* dex_file = method->GetDexFile();
  if (dex_file == nullptr) {
    return false;
  }

  resolved->method = method;
  resolved->dex_begin = dex_file->Begin();
  resolved->dex_size = dex_file->Size();
  resolved->data_begin = dex_file->DataBegin();
  resolved->data_size = dex_file->DataSize();
  resolved->dex_checksum = dex_file->GetHeader().checksum_;
  resolved->method_index = method->GetDexMethodIndex();
  resolved->declaring_class = method->GetDeclaringClassDescriptor();
  resolved->method_name = method->GetName();
  resolved->dex_location = dex_file->GetLocation();
  resolved->native_method = method->IsNative();
  resolved->abstract_method = method->IsAbstract();

  if (!Runtime::Current()->GetClassLinker()->GetDexRegistrationIndex(
          self, *dex_file, &resolved->registration_index)) {
    return false;
  }

  const dex::CodeItem* code_item = method->GetCodeItem();
  if (code_item != nullptr) {
    CodeItemDataAccessor accessor(*dex_file, code_item);
    const uint8_t* code_begin = reinterpret_cast<const uint8_t*>(code_item);
    const uint8_t* code_end = reinterpret_cast<const uint8_t*>(accessor.CodeItemDataEnd());
    if (code_end >= code_begin) {
      resolved->code_begin = code_begin;
      resolved->code_size = static_cast<size_t>(code_end - code_begin);
    }
  }
  return true;
}

bool ResolveCode(JNIEnv* env,
                 jobject callback,
                 const char* method_name,
                 const char* descriptor,
                 ResolvedCode* resolved) {
  jclass callback_class = env->GetObjectClass(callback);
  if (callback_class == nullptr) {
    env->ExceptionClear();
    return false;
  }
  jmethodID method_id = env->GetMethodID(callback_class, method_name, descriptor);
  env->DeleteLocalRef(callback_class);
  if (method_id == nullptr) {
    env->ExceptionClear();
    return false;
  }

  ScopedObjectAccess soa(env);
  ArtMethod* method = jni::DecodeArtMethod(method_id);
  return ResolveArtMethodCode(soa.Self(), method, resolved);
}

struct MeasuredMethod {
  ArtMethod* method = nullptr;
  std::string method_id;
  std::string dex_hash;
  std::string source_dex_hash;
  std::string code_hash;
  std::string declaring_class;
  std::string method_name;
  std::string dex_location;
  uint32_t method_index = dex::kDexNoIndex;
  uint32_t dex_checksum = 0u;
  bool native_method = false;
  bool application_code = false;
};

bool MeasureArtMethod(ArtMethod* method, MeasuredMethod* measured)
    REQUIRES_SHARED(Locks::mutator_lock_) {
  ResolvedCode resolved;
  if (!ResolveArtMethodCode(Thread::Current(), method, &resolved) ||
      resolved.method_index == dex::kDexNoIndex || resolved.dex_begin == nullptr ||
      resolved.dex_size == 0u || resolved.dex_size > kMaxMeasuredDexBytes) {
    return false;
  }

  const std::string dex_key = android::base::StringPrintf(
      "continuation:%" PRIu64 ":%p:%zu:%p:%zu:%08x:%s",
      resolved.registration_index,
      resolved.dex_begin,
      resolved.dex_size,
      resolved.data_begin,
      resolved.data_size,
      resolved.dex_checksum,
      resolved.dex_location.c_str());
  std::string dex_hash;
  if (!LookupHash(dex_key, &g_dex_hash_cache, &dex_hash)) {
    dex_hash = Sha256DexSections(
        resolved.dex_begin, resolved.dex_size, resolved.data_begin, resolved.data_size);
    if (!CacheHash(dex_key, dex_hash, kMaxDexCacheEntries, &g_dex_hash_cache)) {
      return false;
    }
  }

  const std::string source_key = dex_key + ":source";
  std::string source_dex_hash;
  if (!LookupHash(source_key, &g_dex_hash_cache, &source_dex_hash)) {
    source_dex_hash = Sha256(resolved.dex_begin, resolved.dex_size);
    if (!CacheHash(source_key, source_dex_hash, kMaxDexCacheEntries, &g_dex_hash_cache)) {
      return false;
    }
  }

  const bool application_code =
      method->GetDeclaringClass() != nullptr &&
      !method->GetDeclaringClass()->IsBootStrapClassLoaded();
  std::string code_hash;
  if (resolved.native_method) {
    code_hash = "native-unmeasured";
  } else if (resolved.abstract_method || resolved.code_begin == nullptr ||
             resolved.code_size == 0u || resolved.code_size > kMaxMeasuredCodeBytes) {
    return false;
  } else {
    const std::string method_key = android::base::StringPrintf(
        "%s:%u:%p:%zu",
        dex_key.c_str(), resolved.method_index, resolved.code_begin, resolved.code_size);
    // Application DEX can come from writable/in-memory class loaders.  Re-hash the
    // actual CodeItem every time it participates in a continuation so a method that
    // was measured once and then modified cannot retain an old allowlisted identity.
    // Boot-classpath code is system-owned and remains safe to cache.
    if (application_code) {
      code_hash = Sha256(resolved.code_begin, resolved.code_size);
    } else if (!LookupHash(method_key, &g_method_hash_cache, &code_hash)) {
      code_hash = Sha256(resolved.code_begin, resolved.code_size);
      if (!CacheHash(method_key, code_hash, kMaxMethodCacheEntries, &g_method_hash_cache)) {
        return false;
      }
    }
  }

  measured->method = method;
  measured->dex_hash = dex_hash;
  measured->source_dex_hash = source_dex_hash;
  measured->code_hash = code_hash;
  measured->declaring_class = resolved.declaring_class;
  measured->method_name = resolved.method_name;
  measured->dex_location = resolved.dex_location;
  measured->method_index = resolved.method_index;
  measured->dex_checksum = resolved.dex_checksum;
  measured->native_method = resolved.native_method;
  measured->application_code = application_code;
  measured->method_id = Sha256(android::base::StringPrintf(
      "ARGUS/METHOD/v1|%s|%s|%u|%s|%s|%s",
      dex_hash.c_str(),
      source_dex_hash.c_str(),
      resolved.method_index,
      code_hash.c_str(),
      resolved.declaring_class.c_str(),
      resolved.method_name.c_str()));
  return true;
}

constexpr size_t kMaxContinuationDepth = 8u;
constexpr size_t kMaxContinuationEdges = 512u;
constexpr size_t kMaxEnforcedSequenceEdges = 128u;
constexpr uint32_t kSelectorTimingSampleStride = 256u;

struct ArgusContinuationContext {
  uint64_t token = 0u;
  ArtMethod* root_method = nullptr;
  ShadowFrame* root_frame = nullptr;
  ArtMethod* selector_method = nullptr;
  std::string domain;
  std::string operation;
  std::string entry_code_id;
  std::string binding_id;
  std::string dci;
  std::string trace_hash;
  std::string selector_method_id;
  std::string selector_install_set;
  std::string selector_site_id;
  std::string selector_edge_id;
  std::string selector_recipe_name;
  std::string selector_branch_trace;
  std::string dci_plus;
  std::string credential_expected_dci;
  std::string credential_expected_selector_edge;
  std::vector<std::string> expected_edges;
  std::string decision_reason;
  int32_t input_event_id = -1;
  int32_t input_source = 0;
  uint32_t edge_count = 0u;
  uint32_t selector_hook_calls = 0u;
  uint32_t selector_candidate_calls = 0u;
  uint32_t selector_branch_count = 0u;
  uint64_t identity_begin_wall_ns = 0u;
  uint64_t identity_ready_wall_ns = 0u;
  uint64_t base_acquire_cpu_ns = 0u;
  uint64_t selector_sample_cpu_ns = 0u;
  uint64_t selector_candidate_cpu_ns = 0u;
  uint64_t credential_read_cpu_ns = 0u;
  uint64_t credential_compare_cpu_ns = 0u;
  uint32_t selector_timing_samples = 0u;
  bool entered = false;
  bool enforce = false;
  bool frontier_seen = false;
  bool authorized = false;
  bool denied = false;
  bool aborted = false;
  bool completed = false;
  bool force_interpreter = false;
  bool selector_policy = false;
  bool selector_seen = false;
  bool selector_pre_resolved = false;
  bool selector_dispatch_path = false;
  bool credential_match_requested = false;
  bool credential_base_match = false;
  bool credential_selector_match = false;
  bool credential_match_finalized = false;
};

class ScopedSelectorAcquireTimer {
 public:
  explicit ScopedSelectorAcquireTimer(ArgusContinuationContext* context)
      : context_(context),
        start_cpu_ns_(0u),
        candidate_(false) {
    if (context_ != nullptr && context_->selector_policy && !context_->selector_seen) {
      ++context_->selector_hook_calls;
      if ((context_->selector_hook_calls - 1u) % kSelectorTimingSampleStride == 0u) {
        start_cpu_ns_ = ThreadCpuNanoTime();
      }
    }
  }

  ~ScopedSelectorAcquireTimer() { Stop(); }

  void MarkCandidate() {
    if (context_ != nullptr) {
      ++context_->selector_candidate_calls;
      candidate_ = true;
      // Candidate calls perform method measurement and hashing and are rare;
      // time all of them.  Fast non-candidate hook filtering is sampled to
      // avoid making hundreds of thousands of clock reads perturb the value
      // being measured.
      if (start_cpu_ns_ == 0u) {
        start_cpu_ns_ = ThreadCpuNanoTime();
      }
    }
  }

  void Stop() {
    if (start_cpu_ns_ == 0u) {
      return;
    }
    const uint64_t elapsed = ThreadCpuNanoTime() - start_cpu_ns_;
    if (candidate_) {
      context_->selector_candidate_cpu_ns += elapsed;
    } else {
      context_->selector_sample_cpu_ns += elapsed;
      ++context_->selector_timing_samples;
    }
    start_cpu_ns_ = 0u;
  }

 private:
  ArgusContinuationContext* context_;
  uint64_t start_cpu_ns_;
  bool candidate_;
};

thread_local std::vector<ArgusContinuationContext> g_continuation_stack;
std::atomic<uint64_t> g_next_continuation_token(1u);

ArgusContinuationContext* CurrentContinuation() {
  return g_continuation_stack.empty() ? nullptr : &g_continuation_stack.back();
}

bool IsContinuationTracingEnabled() {
  return android::base::GetBoolProperty("debug.argus.continuation_trace", false);
}

bool PrepareContinuationInterpreter(JNIEnv* env, ArtMethod* root_method) {
  Thread* self = Thread::Current();
  if (env == nullptr || self == nullptr || root_method == nullptr) {
    return false;
  }
  instrumentation::Instrumentation* instrumentation =
      Runtime::Current()->GetInstrumentation();
  {
    ScopedObjectAccess soa(env);
    if (!instrumentation->IsDeoptimized(root_method)) {
      // Deoptimize only the certified callback entry, not zygote/system_server or the
      // whole application.  Once inside that switch-interpreter frame, the per-thread
      // force count keeps dynamically selected descendants in the switch interpreter.
      ScopedThreadSuspension sts(self, ThreadState::kSuspended);
      gc::ScopedGCCriticalSection gcs(self,
                                      gc::kGcCauseInstrumentation,
                                      gc::kCollectorTypeInstrumentation);
      ScopedSuspendAll ssa("Argus callback deoptimization");
      if (!instrumentation->IsDeoptimized(root_method)) {
        instrumentation->Deoptimize(root_method);
      }
    }
  }
  {
    MutexLock mu(self, *Locks::thread_list_lock_);
    self->IncrementForceInterpreterCount();
  }
  return true;
}

void ReleaseContinuationInterpreter() {
  Thread* self = Thread::Current();
  if (self == nullptr) {
    return;
  }
  MutexLock mu(self, *Locks::thread_list_lock_);
  if (self->ForceInterpreterCount() == 0u) {
    LOG(ERROR) << "ArgusContinuation force-interpreter underflow prevented";
    return;
  }
  self->DecrementForceInterpreterCount();
}

bool EvaluateContinuationEdge(ArgusContinuationContext* context,
                              const char* kind,
                              const std::string& site_id,
                              const std::string& edge_id,
                              const std::string& details) {
  if (context == nullptr || context->denied) {
    return context == nullptr;
  }
  if (context->edge_count >= kMaxContinuationEdges) {
    if (context->enforce) {
      context->denied = true;
      context->decision_reason = "edge_budget_exceeded";
      return false;
    }
    return true;
  }
  ++context->edge_count;
  context->trace_hash = Sha256(
      "ARGUS/TRACE-STEP/v1|" + context->trace_hash + '|' + edge_id);

  bool edge_allowed = false;
  if (context->enforce) {
    context->frontier_seen = true;
    const size_t sequence_index = context->edge_count - 1u;
    edge_allowed = sequence_index < context->expected_edges.size() &&
        edge_id == context->expected_edges[sequence_index];
    if (!edge_allowed) {
      context->denied = true;
      context->decision_reason = sequence_index >= context->expected_edges.size()
          ? "unexpected_extra_edge" : "continuation_sequence_mismatch";
    } else if (context->edge_count == context->expected_edges.size()) {
      context->authorized = true;
    }
  }

  LOG(INFO) << "ArgusContinuation Edge"
            << " token=" << context->token
            << " eventId=" << context->input_event_id
            << " operation=" << context->operation
            << " kind=" << kind
            << " ordinal=" << context->edge_count
            << " siteId=" << site_id
            << " edgeId=" << edge_id
            << " traceHash=" << context->trace_hash
            << " sequenceIndex=" << (context->edge_count - 1u)
            << " decision=" << (context->denied ? "deny" : edge_allowed ?
                                  (context->authorized ? "allow_complete" : "allow_prefix") :
                                  "observe")
            << ' ' << details;
  return !context->denied;
}

}  // namespace

bool ArgusContinuationObserveCall(ArtMethod* caller,
                                  uint32_t dex_pc,
                                  ArtMethod* actual_target,
                                  int invoke_type) {
  ArgusContinuationContext* context = CurrentContinuation();
  if (context == nullptr) {
    return true;
  }
  if (!context->entered) {
    // The exact callback frame, rather than a possibly compiled framework invoke,
    // owns root entry.  Its Preamble will arm the frame boundary.
    return true;
  }
  if (context->denied) {
    return false;
  }

  ScopedSelectorAcquireTimer selector_timer(context);

  if (context->selector_policy && (caller == nullptr ||
      caller->GetCanonicalMethod() != context->selector_method || context->selector_seen)) {
    // DCI+ adds exactly one selector: the listener's first outbound APK-local call, not every
    // framework/helper/state-dependent call made later in the continuation.
    return true;
  }

  if (context->selector_policy && context->selector_install_set.empty()) {
    // Framework callbacks are not silently treated as target-APK selectors.
    return true;
  }

  if (context->selector_policy) {
    selector_timer.MarkCandidate();
  }

  // A dispatch-path identity is meaningful only after the callback has made at least one
  // control-flow decision.  In particular, javac may emit synthetic field accessors before
  // the first if/switch; treating that plumbing as the selector would merely reproduce the
  // shared-listener collision.
  if (context->selector_dispatch_path && context->selector_branch_count == 0u) {
    return true;
  }

  const bool caller_is_application = caller != nullptr && caller->GetDeclaringClass() != nullptr &&
      !caller->GetDeclaringClass()->IsBootStrapClassLoaded();
  const bool target_is_application = actual_target != nullptr &&
      actual_target->GetDeclaringClass() != nullptr &&
      !actual_target->GetDeclaringClass()->IsBootStrapClassLoaded();
  if (!caller_is_application && !target_is_application) {
    return true;
  }

  MeasuredMethod caller_measurement;
  MeasuredMethod target_measurement;
  if (!MeasureArtMethod(caller, &caller_measurement) ||
      !MeasureArtMethod(actual_target, &target_measurement)) {
    LOG(WARNING) << "ArgusContinuation unresolved call edge"
                 << " token=" << context->token
                 << " dexPc=" << dex_pc;
    if (context->enforce) {
      context->denied = true;
      context->decision_reason = "unresolved_call_edge";
      return false;
    }
    return true;
  }


  // Skip compiler-generated access bridges while looking for the post-dispatch call frontier.
  // Their call sites are code provenance, but they do not delimit an application effect and can
  // occur between two branches of the same source-level dispatch chain.
  const bool target_is_synthetic_accessor =
      target_measurement.method_name.compare(0u, 8u, "-$$Nest$") == 0 ||
      target_measurement.method_name.compare(0u, 7u, "access$") == 0;
  if (context->selector_dispatch_path && target_is_synthetic_accessor) {
    return true;
  }

  const bool caller_in_selector_install =
      InstallSetKey(caller_measurement.dex_location) == context->selector_install_set;
  const bool target_in_selector_install =
      InstallSetKey(target_measurement.dex_location) == context->selector_install_set;
  const bool dispatch_path_frontier = context->selector_policy &&
      context->selector_dispatch_path && context->selector_branch_count > 0u &&
      caller_in_selector_install;
  if (context->selector_policy &&
      !(caller_in_selector_install && target_in_selector_install) &&
      !dispatch_path_frontier) {
    // The ordinary selector requires an APK-local dynamic call edge.  A certified shared
    // listener may instead commit its already-observed control-flow dispatch path at the first
    // subsequent call frontier; the callee may be a framework effect API.
    return true;
  }

  const std::string site_id = dispatch_path_frontier
      ? Sha256(android::base::StringPrintf(
            "ARGUS/DISPATCH-FRONTIER-SITE/v1|%s|%u|%d",
            caller_measurement.method_id.c_str(), dex_pc, invoke_type))
      : context->selector_policy
      ? Sha256(android::base::StringPrintf(
            "ARGUS/SELECTOR-CALL-SITE/v1|%s|%u|%d",
            caller_measurement.method_id.c_str(), dex_pc, invoke_type))
      : Sha256(android::base::StringPrintf(
            "ARGUS/CALL-SITE/v1|%s|%s|%u|%d",
            context->entry_code_id.c_str(),
            caller_measurement.method_id.c_str(),
            dex_pc,
            invoke_type));
  const std::string edge_id = dispatch_path_frontier
      ? Sha256("ARGUS/DISPATCH-PATH-EDGE/v1|" + context->selector_branch_trace + '|' +
               site_id + '|' + target_measurement.method_id)
      : context->selector_policy
      ? Sha256(android::base::StringPrintf(
            "ARGUS/SELECTOR-CALL-EDGE/v1|%s|%s",
            site_id.c_str(), target_measurement.method_id.c_str()))
      : Sha256(android::base::StringPrintf(
            "ARGUS/CALL-EDGE/v1|%s|%s",
            site_id.c_str(), target_measurement.method_id.c_str()));
  if (context->selector_policy) {
    context->selector_seen = true;
    context->selector_site_id = site_id;
    context->selector_edge_id = edge_id;
    context->dci_plus = Sha256(
        "ARGUS/DCI-PLUS/v3|" + context->dci + '|' + context->selector_edge_id);
    context->identity_ready_wall_ns = NanoTime();
    if (context->credential_match_requested) {
      const uint64_t compare_start_ns = ThreadCpuNanoTime();
      context->credential_selector_match =
          context->credential_expected_selector_edge == context->selector_edge_id;
      context->credential_match_finalized = true;
      context->credential_compare_cpu_ns += ThreadCpuNanoTime() - compare_start_ns;
    }
  }
  // Exclude diagnostic string formatting and log I/O from the requested DCI+ acquisition time.
  selector_timer.Stop();
  const std::string details = android::base::StringPrintf(
      "caller=%s->%s callerIndex=%u dexPc=%u invokeType=%d "
      "target=%s->%s targetIndex=%u targetNative=%s targetOrigin=%s "
      "dispatchBranches=%u",
      caller_measurement.declaring_class.c_str(),
      caller_measurement.method_name.c_str(),
      caller_measurement.method_index,
      dex_pc,
      invoke_type,
      target_measurement.declaring_class.c_str(),
      target_measurement.method_name.c_str(),
      target_measurement.method_index,
      target_measurement.native_method ? "yes" : "no",
      SanitizeForLog(target_measurement.dex_location).c_str(),
      context->selector_branch_count);
  return EvaluateContinuationEdge(
      context,
      dispatch_path_frontier ? "selector_dispatch_path" :
          context->selector_policy ? "selector_call" : "call",
      site_id,
      edge_id,
      details);
}

void ArgusContinuationMethodPreamble(ShadowFrame* frame, ArtMethod* method) {
  ArgusContinuationContext* context = CurrentContinuation();
  if (context == nullptr || context->entered || frame == nullptr ||
      method != context->root_method) {
    return;
  }
  context->entered = true;
  context->root_frame = frame;
  LOG(INFO) << "ArgusContinuation RootEnter"
            << " token=" << context->token
            << " eventId=" << context->input_event_id
            << " operation=" << context->operation
            << " entryCodeId=" << context->entry_code_id
            << " bindingId=" << context->binding_id
            << " source=callback_preamble";
}

bool ArgusContinuationObserveBranch(ArtMethod* method,
                                    uint32_t dex_pc,
                                    int32_t selected_offset,
                                    bool selector_candidate) {
  ArgusContinuationContext* context = CurrentContinuation();
  if (context != nullptr && context->selector_policy) {
    ScopedSelectorAcquireTimer selector_timer(context);
    if (!context->selector_dispatch_path || context->selector_seen || !selector_candidate ||
        method == nullptr || method->GetCanonicalMethod() != context->selector_method) {
      return true;
    }
    selector_timer.MarkCandidate();
    MeasuredMethod measurement;
    if (!MeasureArtMethod(method, &measurement) ||
        InstallSetKey(measurement.dex_location) != context->selector_install_set) {
      LOG(WARNING) << "ArgusContinuation unresolved dispatch branch"
                   << " token=" << context->token
                   << " dexPc=" << dex_pc;
      if (context->enforce) {
        context->denied = true;
        context->decision_reason = "unresolved_dispatch_branch";
        return false;
      }
      return true;
    }
    const int64_t successor = static_cast<int64_t>(dex_pc) + selected_offset;
    const std::string site_id = Sha256(android::base::StringPrintf(
        "ARGUS/DISPATCH-BRANCH-SITE/v1|%s|%u",
        measurement.method_id.c_str(), dex_pc));
    const std::string edge_id = Sha256(android::base::StringPrintf(
        "ARGUS/DISPATCH-BRANCH-EDGE/v1|%s|%" PRId64,
        site_id.c_str(), successor));
    context->selector_branch_trace = Sha256(
        "ARGUS/DISPATCH-PATH-STEP/v1|" + context->selector_branch_trace + '|' + edge_id);
    ++context->selector_branch_count;
    selector_timer.Stop();
    LOG(INFO) << "ArgusContinuation SelectorBranch"
              << " token=" << context->token
              << " eventId=" << context->input_event_id
              << " operation=" << context->operation
              << " ordinal=" << context->selector_branch_count
              << " method=" << measurement.declaring_class << "->"
              << measurement.method_name
              << " methodIndex=" << measurement.method_index
              << " dexPc=" << dex_pc
              << " successor=" << successor
              << " siteId=" << site_id
              << " edgeId=" << edge_id
              << " pathHash=" << context->selector_branch_trace;
    return true;
  }
  if (context == nullptr || !context->entered) {
    return true;
  }
  if (context->denied) {
    return false;
  }
  if (method == nullptr || method->GetDeclaringClass() == nullptr ||
      method->GetDeclaringClass()->IsBootStrapClassLoaded()) {
    return true;
  }

  MeasuredMethod measurement;
  if (!MeasureArtMethod(method, &measurement)) {
    LOG(WARNING) << "ArgusContinuation unresolved branch edge"
                 << " token=" << context->token
                 << " dexPc=" << dex_pc;
    if (context->enforce) {
      context->denied = true;
      context->decision_reason = "unresolved_branch_edge";
      return false;
    }
    return true;
  }

  const int64_t successor = static_cast<int64_t>(dex_pc) + selected_offset;
  const std::string site_id = Sha256(android::base::StringPrintf(
      "ARGUS/BRANCH-SITE/v1|%s|%s|%u",
      context->entry_code_id.c_str(), measurement.method_id.c_str(), dex_pc));
  const std::string edge_id = Sha256(android::base::StringPrintf(
      "ARGUS/BRANCH-EDGE/v1|%s|%" PRId64,
      site_id.c_str(), successor));
  const std::string details = android::base::StringPrintf(
      "method=%s->%s methodIndex=%u dexPc=%u selectedOffset=%d successor=%" PRId64,
      measurement.declaring_class.c_str(),
      measurement.method_name.c_str(),
      measurement.method_index,
      dex_pc,
      selected_offset,
      successor);
  return EvaluateContinuationEdge(context, "branch", site_id, edge_id, details);
}

void ArgusContinuationMethodExit(ShadowFrame* frame) {
  ArgusContinuationContext* context = CurrentContinuation();
  if (context == nullptr || !context->entered || frame != context->root_frame) {
    return;
  }
  context->entered = false;
  context->root_frame = nullptr;
  context->completed = true;
  if (context->enforce && !context->authorized) {
    context->denied = true;
    if (context->decision_reason.empty()) {
      context->decision_reason = context->edge_count < context->expected_edges.size()
          ? "continuation_sequence_incomplete" : "continuation_not_authorized";
    }
  }
  LOG(INFO) << "ArgusContinuation RootExit"
            << " token=" << context->token
            << " operation=" << context->operation
            << " decision=" << (context->denied ? "deny" : "sealed")
            << " traceHash=" << context->trace_hash;
}

bool ArgusContinuationShouldForceReturn(ShadowFrame* frame) {
  ArgusContinuationContext* context = CurrentContinuation();
  if (context == nullptr || !context->entered || !context->denied) {
    return false;
  }
  if (frame == context->root_frame) {
    context->entered = false;
    context->root_frame = nullptr;
    context->aborted = true;
    context->completed = true;
    LOG(INFO) << "ArgusContinuation RootAbort"
              << " token=" << context->token
              << " operation=" << context->operation
              << " reason=" << context->decision_reason
              << " traceHash=" << context->trace_hash;
  }
  return true;
}

extern "C" JNIEXPORT jlong JNICALL
ArtArgusContinuationBegin(JNIEnv* env,
                          jobject callback,
                          jstring method_name,
                          jstring descriptor,
                          jobject binding_callback,
                          jstring binding_method_name,
                          jstring binding_descriptor,
                          jstring domain,
                          jint input_event_id,
                          jint input_source,
                          jstring operation,
                          jstring route,
                          jstring arguments_hash) {
  if (env == nullptr || callback == nullptr || method_name == nullptr || descriptor == nullptr ||
      binding_callback == nullptr || binding_method_name == nullptr ||
      binding_descriptor == nullptr ||
      (input_source != 2 && input_source != 3)) {
    return 0;
  }

  ScopedUtfChars method_chars(env, method_name);
  ScopedUtfChars descriptor_chars(env, descriptor);
  ScopedUtfChars binding_method_chars(env, binding_method_name);
  ScopedUtfChars binding_descriptor_chars(env, binding_descriptor);
  ScopedUtfChars domain_chars(env, domain);
  ScopedUtfChars operation_chars(env, operation);
  ScopedUtfChars route_chars(env, route);
  ScopedUtfChars arguments_chars(env, arguments_hash);
  if (!method_chars.Valid() || !descriptor_chars.Valid() || !domain_chars.Valid() ||
      !binding_method_chars.Valid() || !binding_descriptor_chars.Valid() ||
      !operation_chars.Valid() || !route_chars.Valid() || !arguments_chars.Valid()) {
    env->ExceptionClear();
    return 0;
  }

  const bool trace_requested = IsContinuationTracingEnabled();
  const int configured_trace_uid = android::base::GetIntProperty<int>(
      "debug.argus.continuation_trace_uid", -1);
  const bool trace_scope = trace_requested && configured_trace_uid >= 0 &&
      static_cast<uid_t>(configured_trace_uid) == getuid();
  const bool enforce_requested = android::base::GetBoolProperty(
      "debug.argus.continuation_enforce", false);
  const int configured_uid = android::base::GetIntProperty<int>(
      "debug.argus.continuation_uid", -1);
  const bool raw_observer = std::strcmp(operation_chars.Get(""), "raw_touch") == 0;
  const bool authenticated_agent_source = input_source == 2 || input_source == 3;
  const bool enforce_scope = enforce_requested && authenticated_agent_source && !raw_observer &&
      configured_uid >= 0 &&
      static_cast<uid_t>(configured_uid) == getuid();
  if (!trace_scope && !enforce_scope) {
    return 0;
  }
  for (ArgusContinuationContext& active : g_continuation_stack) {
    if (!active.completed && active.input_event_id == input_event_id &&
        active.input_source == input_source &&
        active.operation == operation_chars.Get("unknown")) {
      // Framework wrappers may nest around the same operation (for example the invariant
      // performClick virtual-dispatch root around the listener callback).  Keep one ART scope so
      // the parent observes the actual override, selector, and listener as a single continuation.
      LOG(INFO) << "ArgusContinuation FoldNested"
                << " parentToken=" << active.token
                << " eventId=" << input_event_id
                << " operation=" << operation_chars.Get("unknown")
                << " decision=inherit_parent";
      return 0;
    }
    if (active.enforce && !active.completed) {
      active.denied = true;
      active.decision_reason = "nested_scope_not_certified";
      LOG(WARNING) << "ArgusContinuation nested scope rejected"
                   << " parentToken=" << active.token
                   << " childOperation=" << operation_chars.Get("unknown")
                   << " decision=deny";
      return -1;
    }
  }
  if (g_continuation_stack.size() >= kMaxContinuationDepth) {
    LOG(WARNING) << "ArgusContinuation nesting limit reached";
    return enforce_scope ? -1 : 0;
  }

  ResolvedCode resolved;
  if (!ResolveCode(env,
                   callback,
                   method_chars.Get(""),
                   descriptor_chars.Get(""),
                   &resolved) ||
      resolved.method == nullptr || resolved.native_method || resolved.abstract_method) {
    LOG(WARNING) << "ArgusContinuation root unresolved or unsupported"
                 << " method=" << method_chars.Get("<unknown>");
    return enforce_scope ? -1 : 0;
  }
  MeasuredMethod root;
  {
    ScopedObjectAccess soa(env);
    if (!MeasureArtMethod(resolved.method, &root)) {
      return enforce_scope ? -1 : 0;
    }
  }
  const uint64_t base_acquire_wall_start_ns = NanoTime();
  const uint64_t base_acquire_cpu_start_ns = ThreadCpuNanoTime();
  ResolvedCode selector_resolved;
  if (!ResolveCode(env,
                   binding_callback,
                   binding_method_chars.Get(""),
                   binding_descriptor_chars.Get(""),
                   &selector_resolved) ||
      selector_resolved.method == nullptr || selector_resolved.native_method ||
      selector_resolved.abstract_method) {
    LOG(WARNING) << "ArgusContinuation selector unresolved or unsupported"
                 << " method=" << binding_method_chars.Get("<unknown>");
    return enforce_scope ? -1 : 0;
  }
  MeasuredMethod selector;
  ArtMethod* selector_method = nullptr;
  {
    ScopedObjectAccess soa(env);
    if (!MeasureArtMethod(selector_resolved.method, &selector)) {
      return enforce_scope ? -1 : 0;
    }
    selector_method = selector_resolved.method->GetCanonicalMethod();
  }
  const std::string original_dci = android::base::StringPrintf(
      "%s-%08x-%u",
      domain_chars.Get("unknown"), selector.dex_checksum, selector.method_index);
  const std::string base_dci_plus = Sha256("ARGUS/DCI-PLUS/v3|" + original_dci);
  const std::string certified_dialog_recipe =
      android::base::GetBoolProperty("debug.argus.certified_dialog_selector", false)
          ? CertifiedDialogRecipe(route_chars.Get("")) : std::string();
  const std::string certified_self_route_recipe =
      android::base::GetBoolProperty("debug.argus.self_route", false)
          ? CertifiedSelfRouteRecipe(route_chars.Get("")) : std::string();
  const bool dispatch_path_selector =
      android::base::GetBoolProperty("debug.argus.dispatch_path_selector", false) &&
      std::strcmp(route_chars.Get(""), "ARGUS/DISPATCH-PATH/v1") == 0;
  const bool certified_self_route_eligible = !certified_self_route_recipe.empty() &&
      selector.declaring_class == "Landroid/view/View;" &&
      selector.method_name == "performClick";
  const std::string certified_selector_recipe = !certified_dialog_recipe.empty()
      ? certified_dialog_recipe : certified_self_route_eligible
          ? certified_self_route_recipe : std::string();
  const std::string certified_selector_recipe_name = !certified_dialog_recipe.empty()
      ? "certified_dialog_delegate_v4" : certified_self_route_eligible
          ? "certified_self_route_v1" : std::string();
  const bool selector_pre_resolved =
      std::strcmp(operation_chars.Get("unknown"), "click") == 0 &&
      !certified_selector_recipe.empty();
  const std::string certified_selector_site_id = selector_pre_resolved
      ? Sha256("ARGUS/CERTIFIED-BINDING-SITE/v1|" + selector.method_id + '|' +
               certified_selector_recipe)
      : std::string();
  const std::string certified_selector_edge_id = selector_pre_resolved
      ? Sha256("ARGUS/CERTIFIED-BINDING-EDGE/v1|" + certified_selector_site_id + '|' +
               selector.method_id)
      : std::string();
  const std::string initial_dci_plus = selector_pre_resolved
      ? Sha256("ARGUS/DCI-PLUS/v3|" + original_dci + '|' + certified_selector_edge_id)
      : base_dci_plus;
  const uint64_t pre_resolved_ready_wall_ns = selector_pre_resolved ? NanoTime() : 0u;
  const uint64_t base_acquire_cpu_ns = ThreadCpuNanoTime() - base_acquire_cpu_start_ns;

  // A second, frozen-credential pass measures the actual runtime read and
  // comparison needed to distinguish this DCI+.  Observer discovery leaves
  // this disabled, so credential I/O is never folded into acquisition time.
  const uint64_t credential_read_start_ns = ThreadCpuNanoTime();
  const bool credential_match_requested =
      std::strcmp(operation_chars.Get("unknown"), "click") == 0 &&
      android::base::GetBoolProperty("debug.argus.dci_match", false);
  std::string credential_expected_dci;
  std::string credential_expected_selector_edge;
  if (credential_match_requested) {
    credential_expected_dci = android::base::GetProperty(
        "debug.argus.dci_expected", "");
    credential_expected_selector_edge = android::base::GetProperty(
        "debug.argus.dci_selector_expected", "");
  }
  const uint64_t credential_read_cpu_ns = credential_match_requested
      ? ThreadCpuNanoTime() - credential_read_start_ns : 0u;
  const uint64_t credential_compare_start_ns = ThreadCpuNanoTime();
  const bool credential_base_match = credential_match_requested &&
      credential_expected_dci ==
          Sha256("ARGUS/DCI-CREDENTIAL/v1|" + original_dci) &&
      (credential_expected_selector_edge == "none" ||
       IsLowerHexSha256(credential_expected_selector_edge));
  const bool credential_pre_resolved_selector_match =
      credential_match_requested && selector_pre_resolved &&
      credential_expected_selector_edge == certified_selector_edge_id;
  const uint64_t credential_compare_cpu_ns = credential_match_requested
      ? ThreadCpuNanoTime() - credential_compare_start_ns : 0u;

  const std::string entry_code_id = Sha256(android::base::StringPrintf(
      "ARGUS/ENTRY/v1|%s|%s|%s",
      domain_chars.Get("unknown"),
      root.method_id.c_str(),
      operation_chars.Get("unknown")));
  const std::string binding_id = Sha256(android::base::StringPrintf(
      "ARGUS/BINDING/v1|%s|%s|%s",
      entry_code_id.c_str(),
      route_chars.Get("unknown"),
      arguments_chars.Get("unknown")));
  const std::string expected_binding = android::base::GetProperty(
      "debug.argus.continuation_binding", "");
  const int expected_edge_count = android::base::GetIntProperty<int>(
      "debug.argus.continuation_count", 0);
  std::vector<std::string> expected_edges;
  bool sequence_valid = expected_edge_count > 0 &&
      static_cast<size_t>(expected_edge_count) <= kMaxEnforcedSequenceEdges;
  if (sequence_valid) {
    expected_edges.reserve(static_cast<size_t>(expected_edge_count));
    for (int index = 0; index < expected_edge_count; ++index) {
      const std::string edge = android::base::GetProperty(
          android::base::StringPrintf("debug.argus.continuation_edge.%d", index), "");
      if (!IsLowerHexSha256(edge)) {
        sequence_valid = false;
        break;
      }
      expected_edges.push_back(edge);
    }
  }
  if (enforce_scope &&
      (expected_binding != binding_id ||
       !sequence_valid)) {
    LOG(INFO) << "ArgusContinuation ArmDenied"
              << " uid=" << getuid()
              << " eventId=" << input_event_id
              << " operation=" << operation_chars.Get("unknown")
              << " entryCodeId=" << entry_code_id
              << " bindingId=" << binding_id
              << " interpreterComplete=pending_selective_deopt"
              << " reason=" << (expected_binding != binding_id ? "binding_mismatch" :
                                  "invalid_continuation_sequence");
    return -1;
  }

  Runtime::Current()->SetNonStandardExitsEnabled();
  // Construct in the thread-local stack directly.  Besides avoiding a copy,
  // this keeps the JNI entry's native stack frame below ART's strict limit as
  // the context gains timing and credential fields.
  g_continuation_stack.emplace_back();
  ArgusContinuationContext& context = g_continuation_stack.back();
  context.token = g_next_continuation_token.fetch_add(1u, std::memory_order_relaxed);
  if (context.token == 0u) {
    context.token = g_next_continuation_token.fetch_add(1u, std::memory_order_relaxed);
  }
  context.root_method = resolved.method;
  context.selector_method = selector_method;
  context.domain = domain_chars.Get("unknown");
  context.operation = operation_chars.Get("unknown");
  context.entry_code_id = entry_code_id;
  context.binding_id = binding_id;
  context.dci = original_dci;
  // The original DCI is always the identity floor.  A selector call refines it only when the
  // measured listener actually emits a qualifying call; absence of a selector must not discard
  // the signer + Dex checksum + Dex method identity.
  context.dci_plus = initial_dci_plus;
  context.identity_begin_wall_ns = base_acquire_wall_start_ns;
  context.identity_ready_wall_ns = pre_resolved_ready_wall_ns;
  context.base_acquire_cpu_ns = base_acquire_cpu_ns;
  context.credential_expected_dci = std::move(credential_expected_dci);
  context.credential_expected_selector_edge =
      std::move(credential_expected_selector_edge);
  context.credential_read_cpu_ns = credential_read_cpu_ns;
  context.credential_compare_cpu_ns = credential_compare_cpu_ns;
  context.credential_match_requested = credential_match_requested;
  context.credential_base_match = credential_base_match;
  context.credential_selector_match = credential_pre_resolved_selector_match;
  context.credential_match_finalized = selector_pre_resolved;
  context.selector_method_id = selector.method_id;
  context.selector_install_set = InstallSetKey(selector.dex_location);
  context.selector_policy = context.operation == "click";
  context.selector_seen = selector_pre_resolved;
  context.selector_pre_resolved = selector_pre_resolved;
  context.selector_site_id = certified_selector_site_id;
  context.selector_edge_id = certified_selector_edge_id;
  context.selector_recipe_name = certified_selector_recipe_name;
  context.selector_dispatch_path = dispatch_path_selector &&
      !context.selector_install_set.empty() && !selector_pre_resolved;
  if (context.selector_dispatch_path) {
    context.selector_recipe_name = "dispatch_path_v1";
    context.selector_branch_trace = Sha256(
        "ARGUS/DISPATCH-PATH/v1|" + context.selector_method_id);
  }
  context.trace_hash = Sha256("ARGUS/TRACE/v1|" + binding_id);
  context.expected_edges = std::move(expected_edges);
  context.input_event_id = input_event_id;
  context.input_source = input_source;
  context.enforce = enforce_scope;
  const uint64_t token = context.token;
  if (!PrepareContinuationInterpreter(env, resolved.method)) {
    g_continuation_stack.pop_back();
    LOG(ERROR) << "ArgusContinuation selective interpreter preparation failed"
               << " token=" << token;
    return enforce_scope ? -1 : 0;
  }
  g_continuation_stack.back().force_interpreter = true;

  LOG(INFO) << "ArgusContinuation Arm"
            << " token=" << token
            << " uid=" << getuid()
            << " eventId=" << input_event_id
            << " operation=" << operation_chars.Get("unknown")
            << " entryCodeId=" << entry_code_id
            << " bindingId=" << binding_id
            << " dci=" << g_continuation_stack.back().dci
            << " rootMethodId=" << root.method_id
            << " sourceDexHash=" << root.source_dex_hash
            << " root=" << root.declaring_class << "->" << root.method_name
            << " rootOrigin=" << SanitizeForLog(root.dex_location)
            << " selectorPolicy=" << (g_continuation_stack.back().selector_pre_resolved
                                           ? g_continuation_stack.back().selector_recipe_name
                                           : g_continuation_stack.back().selector_dispatch_path
                                                 ? "dispatch_path_v1"
                                           : g_continuation_stack.back().selector_policy
                                                 ? "first_apk_local_call_v3" : "legacy_v1")
            << " selectorMethodId=" << selector.method_id
            << " selectorOrigin=" << SanitizeForLog(selector.dex_location)
            << " interpreterComplete=selective_callback_thread"
            << " expectedEdges=" << g_continuation_stack.back().expected_edges.size()
            << " enforcement=" << (enforce_scope ? "trace_automaton_allowlist" : "observer");
  return static_cast<jlong>(token);
}

extern "C" JNIEXPORT jint JNICALL
ArtArgusContinuationEnd(jlong token) {
  if (token <= 0) {
    return 0;
  }
  ArgusContinuationContext* context = CurrentContinuation();
  if (context == nullptr) {
    LOG(ERROR) << "ArgusContinuation EndMismatch"
               << " token=" << token
               << " depth=0";
    return -1;
  }
  if (context->token != static_cast<uint64_t>(token) || !context->completed) {
    context->denied = true;
    context->decision_reason = context->token != static_cast<uint64_t>(token)
        ? "mismatched_end" : "premature_end";
    LOG(ERROR) << "ArgusContinuation EndRejected"
               << " token=" << token
               << " expectedToken=" << context->token
               << " completed=" << (context->completed ? "yes" : "no")
               << " decision=deny";
    return -1;
  }

  const char* decision = context->aborted || context->denied ? "deny" :
      context->authorized ? "allow" : context->enforce ? "unresolved" : "observe";
  const std::string reason = context->decision_reason.empty()
      ? (context->enforce && !context->authorized
             ? "continuation_sequence_incomplete"
             : context->authorized ? "allowed_frontier_edge" : "observer_complete")
      : context->decision_reason;
  const uint64_t end_wall_ns = NanoTime();
  const uint64_t identity_ready_wall_ns = context->identity_ready_wall_ns != 0u
      ? context->identity_ready_wall_ns : end_wall_ns;
  const uint64_t dci_ready_elapsed_ns = identity_ready_wall_ns >= context->identity_begin_wall_ns
      ? identity_ready_wall_ns - context->identity_begin_wall_ns : 0u;
  const uint64_t non_candidate_hook_calls =
      context->selector_hook_calls >= context->selector_candidate_calls
          ? context->selector_hook_calls - context->selector_candidate_calls : 0u;
  const uint64_t estimated_non_candidate_cpu_ns = context->selector_timing_samples > 0u
      ? static_cast<uint64_t>(
            (static_cast<long double>(context->selector_sample_cpu_ns) *
             static_cast<long double>(non_candidate_hook_calls)) /
            static_cast<long double>(context->selector_timing_samples))
      : 0u;
  const uint64_t selector_acquire_cpu_ns =
      context->selector_candidate_cpu_ns + estimated_non_candidate_cpu_ns;
  const uint64_t dci_acquire_cpu_ns =
      context->base_acquire_cpu_ns + selector_acquire_cpu_ns;
  if (context->credential_match_requested && !context->credential_match_finalized) {
    const uint64_t compare_start_ns = ThreadCpuNanoTime();
    context->credential_selector_match =
        context->credential_expected_selector_edge == "none";
    context->credential_match_finalized = true;
    context->credential_compare_cpu_ns += ThreadCpuNanoTime() - compare_start_ns;
  }
  const uint64_t dci_distinguish_cpu_ns = dci_acquire_cpu_ns +
      context->credential_read_cpu_ns + context->credential_compare_cpu_ns;
  const bool credential_identity_match = context->credential_match_requested &&
      context->credential_base_match && context->credential_selector_match;
  LOG(INFO) << "ArgusContinuation End"
            << " token=" << context->token
            << " eventId=" << context->input_event_id
            << " operation=" << context->operation
            << " edges=" << context->edge_count
            << " dci=" << context->dci
            << " selectorPolicy=" << (context->selector_pre_resolved
                                           ? context->selector_recipe_name
                                           : context->selector_dispatch_path
                                                 ? "dispatch_path_v1"
                                           : context->selector_policy
                                                 ? "first_apk_local_call_v3" : "legacy_v1")
            << " selectorSeen=" << (context->selector_seen ? "yes" : "no")
            << " selectorSiteId=" << (context->selector_seen
                                           ? context->selector_site_id : "none")
            << " selectorEdgeId=" << (context->selector_seen
                                           ? context->selector_edge_id : "none")
            << " dciplus=" << context->dci_plus
            << " dciAcquireCpuNs=" << dci_acquire_cpu_ns
            << " baseAcquireCpuNs=" << context->base_acquire_cpu_ns
            << " selectorAcquireCpuNs=" << selector_acquire_cpu_ns
            << " selectorSampleCpuNs=" << context->selector_sample_cpu_ns
            << " selectorCandidateCpuNs=" << context->selector_candidate_cpu_ns
            << " selectorTimingSamples=" << context->selector_timing_samples
            << " dciReadyElapsedNs=" << dci_ready_elapsed_ns
            << " identityReadyPhase=" << (context->selector_pre_resolved
                                               ? (context->selector_recipe_name ==
                                                          "certified_self_route_v1"
                                                      ? "certified_self_route_begin"
                                                      : "certified_binding_begin")
                                               : context->selector_seen
                                                     ? (context->selector_dispatch_path
                                                               ? "selector_dispatch_path"
                                                               : "selector_call")
                                                     : "base_fallback_end")
            << " selectorDispatchBranches=" << context->selector_branch_count
            << " selectorHookCalls=" << context->selector_hook_calls
            << " selectorCandidateCalls=" << context->selector_candidate_calls
            << " credentialMatchRequested="
            << (context->credential_match_requested ? "yes" : "no")
            << " credentialReadCpuNs=" << context->credential_read_cpu_ns
            << " credentialCompareCpuNs=" << context->credential_compare_cpu_ns
            << " dciDistinguishCpuNs=" << dci_distinguish_cpu_ns
            << " credentialIdentityMatch="
            << (context->credential_match_requested
                    ? (credential_identity_match ? "yes" : "no") : "not_requested")
            << " entered=" << (context->entered ? "yes" : "no")
            << " frontierSeen=" << (context->frontier_seen ? "yes" : "no")
            << " traceHash=" << context->trace_hash
            << " decision=" << decision
            << " reason=" << reason;
  const jint result = context->aborted || context->denied ? -1 :
      context->authorized ? 1 : 0;
  const bool release_force_interpreter = context->force_interpreter;
  g_continuation_stack.pop_back();
  if (release_force_interpreter) {
    ReleaseContinuationInterpreter();
  }
  return result;
}

extern "C" JNIEXPORT jint JNICALL
ArtArgusDciResolveAndLog(JNIEnv* env,
                        jobject callback,
                                              jstring method_name,
                                              jstring descriptor,
                                              jstring domain,
                                              jint target_id,
                                              jlong target_token,
                                              jint input_event_id,
                                              jint input_source,
                        jstring operation,
                        jstring phase,
                        jstring route,
                        jstring arguments_hash) {
  if (env == nullptr || callback == nullptr || method_name == nullptr || descriptor == nullptr) {
    LOG(WARNING) << "Argus DCI-plus unresolved: missing callback or method signature";
    return -1;
  }

  ScopedUtfChars method_chars(env, method_name);
  ScopedUtfChars descriptor_chars(env, descriptor);
  ScopedUtfChars domain_chars(env, domain);
  ScopedUtfChars operation_chars(env, operation);
  ScopedUtfChars phase_chars(env, phase);
  ScopedUtfChars route_chars(env, route);
  ScopedUtfChars arguments_hash_chars(env, arguments_hash);
  if (!method_chars.Valid() || !descriptor_chars.Valid() || !domain_chars.Valid() ||
      !operation_chars.Valid() || !phase_chars.Valid() || !route_chars.Valid() ||
      !arguments_hash_chars.Valid()) {
    env->ExceptionClear();
    return -1;
  }

  ResolvedCode resolved;
  if (!ResolveCode(env,
                   callback,
                   method_chars.Get(""),
                   descriptor_chars.Get(""),
                   &resolved) ||
      resolved.method_index == dex::kDexNoIndex) {
    LOG(WARNING) << "Argus DCI-plus unresolved: " << method_chars.Get("<unknown>")
                 << descriptor_chars.Get("");
    return -1;
  }

  if (resolved.native_method || resolved.abstract_method || resolved.code_begin == nullptr ||
      resolved.code_size == 0u || resolved.code_size > kMaxMeasuredCodeBytes) {
    LOG(WARNING) << "Argus DCI-plus unresolved: unsupported callback code kind method="
                 << resolved.declaring_class << "->" << resolved.method_name;
    return -1;
  }

  if (resolved.dex_begin == nullptr || resolved.data_begin == nullptr ||
      resolved.dex_size == 0u || resolved.dex_size > kMaxMeasuredDexBytes ||
      resolved.data_size == 0u || resolved.data_size > kMaxMeasuredDexBytes) {
    LOG(WARNING) << "Argus DCI-plus unresolved: invalid DEX measurement bounds location="
                 << SanitizeForLog(resolved.dex_location);
    return -1;
  }

  const std::string dex_key = android::base::StringPrintf(
      "%" PRIu64 ":%p:%zu:%p:%zu:%08x:%s",
      resolved.registration_index,
      resolved.dex_begin,
      resolved.dex_size,
      resolved.data_begin,
      resolved.data_size,
      resolved.dex_checksum,
      resolved.dex_location.c_str());
  std::string dex_hash;
  const bool dex_cache_hit = LookupHash(dex_key, &g_dex_hash_cache, &dex_hash);
  if (!dex_cache_hit) {
    dex_hash = Sha256DexSections(
        resolved.dex_begin, resolved.dex_size, resolved.data_begin, resolved.data_size);
    if (!CacheHash(dex_key, dex_hash, kMaxDexCacheEntries, &g_dex_hash_cache)) {
      LOG(WARNING) << "Argus DCI-plus unresolved: DEX measurement cache capacity exceeded";
      return -1;
    }
  }

  const std::string method_key = android::base::StringPrintf(
      "%s:%u:%p:%zu",
      dex_key.c_str(),
      resolved.method_index,
      resolved.code_begin,
      resolved.code_size);
  std::string code_hash;
  const bool method_cache_hit = LookupHash(method_key, &g_method_hash_cache, &code_hash);
  if (!method_cache_hit) {
    code_hash = Sha256(resolved.code_begin, resolved.code_size);
    if (!CacheHash(method_key, code_hash, kMaxMethodCacheEntries, &g_method_hash_cache)) {
      LOG(WARNING) << "Argus DCI-plus unresolved: method measurement cache capacity exceeded";
      return -1;
    }
  }

  const char* safe_domain = domain_chars.Get("unknown");
  const char* safe_operation = operation_chars.Get("unknown");
  const char* safe_phase = phase_chars.Get("unknown");
  const char* safe_route = route_chars.Get("unknown");
  const std::string dci = android::base::StringPrintf(
      "%s-%08x-%u", safe_domain, resolved.dex_checksum, resolved.method_index);
  const std::string dci_plus_material = android::base::StringPrintf(
      "%s|%s|%s|%u|%s|%s",
      safe_domain,
      dex_hash.c_str(),
      code_hash.c_str(),
      resolved.method_index,
      safe_operation,
      safe_route);
  const std::string dci_plus = Sha256(dci_plus_material);

  // Userdebug-only experiment control. The framework independently limits whether this native
  // decision may affect dispatch to one exact application and agent input. Production replaces
  // this exact-hash debug rule with the system-owned DCI policy table.
  const bool experimental_requested = android::base::GetBoolProperty(
      "debug.argus.experimental_enforce", false);
  const std::string experimental_package = android::base::GetProperty(
      "debug.argus.experimental_package", "");
  const std::string legacy_deny_dci_plus = android::base::GetProperty(
      "debug.argus.experimental_deny_dciplus", "");
  const std::string configured_policy_dci_plus = android::base::GetProperty(
      "debug.argus.experimental_policy_dciplus", "");
  const std::string policy_dci_plus = configured_policy_dci_plus.empty()
      ? legacy_deny_dci_plus : configured_policy_dci_plus;
  const std::string configured_verdict = configured_policy_dci_plus.empty()
      ? "denied" : android::base::GetProperty(
          "debug.argus.experimental_policy_verdict", "denied");
  const std::string expected_domain_prefix = experimental_package + ':';
  const bool agent_input = input_source == 2 || input_source == 3;
  const bool experimental_scope = experimental_requested && agent_input &&
      !experimental_package.empty() && IsLowerHexSha256(policy_dci_plus) &&
      std::strncmp(safe_domain,
                   expected_domain_prefix.c_str(),
                   expected_domain_prefix.size()) == 0;
  constexpr jint kVerdictAllowed = 0;
  constexpr jint kVerdictDenied = 1;
  constexpr jint kVerdictRestricted = 2;
  jint configured_policy_verdict = -1;
  if (configured_verdict == "allowed") {
    configured_policy_verdict = kVerdictAllowed;
  } else if (configured_verdict == "denied") {
    configured_policy_verdict = kVerdictDenied;
  } else if (configured_verdict == "restricted") {
    configured_policy_verdict = kVerdictRestricted;
  }
  const bool policy_match = experimental_scope && policy_dci_plus == dci_plus;
  const jint policy_verdict = !experimental_scope ? kVerdictAllowed
      : policy_match ? configured_policy_verdict : kVerdictAllowed;
  const char* enforcement = experimental_scope ? "experimental" : "observer";
  const char* decision = policy_verdict == kVerdictRestricted ? "restricted"
      : policy_verdict == kVerdictDenied ? "denied"
      : policy_verdict == kVerdictAllowed ? (experimental_scope ? "allowed" : "observe")
      : "unresolved";
  const char* decision_reason = !experimental_scope ? "out_of_scope"
      : !policy_match ? "no_matching_dciplus_rule"
      : configured_policy_verdict < 0 ? "invalid_policy_verdict"
      : "exact_dciplus_rule";

  LOG(INFO) << "Argus DCIPlus"
            << " operation=" << safe_operation
            << " phase=" << safe_phase
            << " source=" << InputSourceName(input_source)
            << " eventId=" << input_event_id
            << " targetId=" << target_id
            << " bindingToken=" << static_cast<uint64_t>(target_token)
            << " dci=" << dci
            << " dciplus=" << dci_plus
            << " dexHash=" << dex_hash
            << " codeHash=" << code_hash
            << " routeHash=" << safe_route
            << " argumentsHash=" << arguments_hash_chars.Get("unknown")
            << " codeOrigin=" << SanitizeForLog(resolved.dex_location)
            << " method=" << resolved.declaring_class << "->" << resolved.method_name
            << descriptor_chars.Get("")
            << " cache=" << (dex_cache_hit ? "dex-hit" : "dex-miss") << ','
            << (method_cache_hit ? "method-hit" : "method-miss")
            << " codeKind=" << (resolved.native_method ? "native" :
                                resolved.abstract_method ? "abstract" : "dex")
            << " measurementScope=loaded-dex-prototype"
            << " enforcement=" << enforcement
            << " decision=" << decision
            << " decisionReason=" << decision_reason;
  return policy_verdict;
}

}  // namespace art
