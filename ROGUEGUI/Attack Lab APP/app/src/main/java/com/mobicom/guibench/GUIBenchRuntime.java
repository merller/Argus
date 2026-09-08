package com.mobicom.guibench;

import android.content.Context;
import android.content.Intent;
import android.net.Uri;
import android.os.Build;
import android.provider.Settings;
import android.util.Log;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.OutputStreamWriter;
import java.nio.charset.StandardCharsets;

public final class GUIBenchRuntime {
    private static final String TAG = "GUIBench";
    private static final String ROOT_DIR = "guibench_runtime";
    private static final String PREFS_NAME = "guibench_runtime";

    private GUIBenchRuntime() {
    }

    public static boolean resetScenario(Context context, String scenario) {
        File dir = scenarioDir(context, scenario);
        return deleteRecursively(dir) && ensureDirectory(dir);
    }

    public static boolean writeScenarioFile(
            Context context,
            String scenario,
            String relativePath,
            String content
    ) {
        File target = new File(scenarioDir(context, scenario), normalizeRelativePath(relativePath));
        File parent = target.getParentFile();
        if (parent != null && !ensureDirectory(parent)) {
            return false;
        }

        try (OutputStreamWriter writer = new OutputStreamWriter(
                new FileOutputStream(target, false),
                StandardCharsets.UTF_8
        )) {
            writer.write(content);
            return true;
        } catch (IOException e) {
            Log.e(TAG, "Failed to write scenario file " + target.getAbsolutePath(), e);
            return false;
        }
    }

    public static boolean appendScenarioFile(
            Context context,
            String scenario,
            String relativePath,
            String content
    ) {
        File target = new File(scenarioDir(context, scenario), normalizeRelativePath(relativePath));
        File parent = target.getParentFile();
        if (parent != null && !ensureDirectory(parent)) {
            return false;
        }

        try (OutputStreamWriter writer = new OutputStreamWriter(
                new FileOutputStream(target, true),
                StandardCharsets.UTF_8
        )) {
            writer.write(content);
            return true;
        } catch (IOException e) {
            Log.e(TAG, "Failed to append scenario file " + target.getAbsolutePath(), e);
            return false;
        }
    }

    public static boolean deleteScenarioEntry(Context context, String scenario, String relativePath) {
        File target = new File(scenarioDir(context, scenario), normalizeRelativePath(relativePath));
        return deleteRecursively(target);
    }

    public static boolean clearScenarioChildren(Context context, String scenario) {
        File dir = scenarioDir(context, scenario);
        if (!dir.exists()) {
            return ensureDirectory(dir);
        }

        File[] children = dir.listFiles();
        if (children == null) {
            return true;
        }

        boolean success = true;
        for (File child : children) {
            success &= deleteRecursively(child);
        }
        return success;
    }

    public static void setFlag(Context context, String scenario, String key, boolean value) {
        context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
                .edit()
                .putBoolean(prefKey(scenario, key), value)
                .apply();
    }

    public static void setValue(Context context, String scenario, String key, String value) {
        context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
                .edit()
                .putString(prefKey(scenario, key), value)
                .apply();
    }

    public static boolean getFlag(Context context, String scenario, String key, boolean defaultValue) {
        return context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
                .getBoolean(prefKey(scenario, key), defaultValue);
    }

    public static String getValue(Context context, String scenario, String key, String defaultValue) {
        return context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
                .getString(prefKey(scenario, key), defaultValue);
    }

    public static Intent newShareIntent(String title, String body) {
        Intent send = new Intent(Intent.ACTION_SEND);
        send.setType("text/plain");
        send.putExtra(Intent.EXTRA_SUBJECT, title);
        send.putExtra(Intent.EXTRA_TEXT, body);
        return Intent.createChooser(send, title);
    }

    public static Intent newAppNotificationSettingsIntent(Context context) {
        Intent intent = new Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS);
        intent.putExtra(Settings.EXTRA_APP_PACKAGE, context.getPackageName());
        intent.putExtra("app_package", context.getPackageName());
        intent.putExtra("app_uid", context.getApplicationInfo().uid);
        return intent;
    }

    public static Intent newAppDetailsIntent(Context context) {
        Intent intent = new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS);
        intent.setData(Uri.fromParts("package", context.getPackageName(), null));
        return intent;
    }

    public static Intent newOverlayPermissionIntent(Context context) {
        return new Intent(
                Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                Uri.parse("package:" + context.getPackageName())
        );
    }

    public static Intent newAccessibilitySettingsIntent() {
        return new Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS);
    }

    public static Intent newOpenDocumentIntent(String suggestedName) {
        Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT);
        intent.addCategory(Intent.CATEGORY_OPENABLE);
        intent.setType("*/*");
        intent.putExtra(Intent.EXTRA_TITLE, suggestedName);
        return intent;
    }

    public static Intent newOpenDocumentTreeIntent() {
        return new Intent(Intent.ACTION_OPEN_DOCUMENT_TREE);
    }

    public static Intent newUnknownSourcesIntent(Context context) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            return new Intent(
                    Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                    Uri.parse("package:" + context.getPackageName())
            );
        }
        return new Intent(Settings.ACTION_SECURITY_SETTINGS);
    }

    public static Intent newDefaultAppsIntent() {
        return new Intent(Settings.ACTION_MANAGE_DEFAULT_APPS_SETTINGS);
    }

    public static Intent newSyncSettingsIntent() {
        return new Intent(Settings.ACTION_SYNC_SETTINGS);
    }

    public static boolean appendAudit(Context context, String scenario, String message) {
        String line = System.currentTimeMillis() + " " + message + "\n";
        return appendScenarioFile(context, scenario, "audit.log", line);
    }

    public static boolean seedScenario(Context context, String scenario, String[][] files) {
        boolean success = resetScenario(context, scenario);
        for (String[] file : files) {
            if (file.length < 2) {
                continue;
            }
            success &= writeScenarioFile(context, scenario, file[0], file[1]);
        }
        return success;
    }

    private static File scenarioDir(Context context, String scenario) {
        return new File(new File(context.getFilesDir(), ROOT_DIR), scenario);
    }

    private static boolean ensureDirectory(File dir) {
        return dir.exists() || dir.mkdirs();
    }

    private static String normalizeRelativePath(String relativePath) {
        return relativePath.replace('/', File.separatorChar);
    }

    private static String prefKey(String scenario, String key) {
        return scenario + "." + key;
    }

    private static boolean deleteRecursively(File target) {
        if (target == null || !target.exists()) {
            return true;
        }

        File[] children = target.listFiles();
        boolean success = true;
        if (children != null) {
            for (File child : children) {
                success &= deleteRecursively(child);
            }
        }

        if (!target.delete()) {
            Log.w(TAG, "Unable to delete " + target.getAbsolutePath());
            return false;
        }
        return success;
    }
}
