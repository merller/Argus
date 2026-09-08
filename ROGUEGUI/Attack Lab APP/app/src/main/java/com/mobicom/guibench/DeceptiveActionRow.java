package com.mobicom.guibench;

import android.content.Context;
import android.graphics.Typeface;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.View;
import android.view.accessibility.AccessibilityNodeInfo;
import android.widget.LinearLayout;
import android.widget.TextView;

public class DeceptiveActionRow extends LinearLayout {
    private final TextView titleView;
    private final TextView subtitleView;
    private final TextView badgeView;

    private CharSequence accessibilityTitle;
    private CharSequence accessibilitySummary;
    private CharSequence accessibilityRole;

    public DeceptiveActionRow(Context context) {
        super(context);
        setOrientation(VERTICAL);
        setPadding(dp(14), dp(14), dp(14), dp(14));
        setClickable(true);
        setFocusable(true);
        setImportantForAccessibility(View.IMPORTANT_FOR_ACCESSIBILITY_YES);
        setBackgroundColor(0x221144AA);

        LinearLayout topRow = new LinearLayout(context);
        topRow.setOrientation(HORIZONTAL);
        topRow.setGravity(Gravity.CENTER_VERTICAL);

        titleView = new TextView(context);
        titleView.setTextSize(TypedValue.COMPLEX_UNIT_SP, 17);
        titleView.setTypeface(titleView.getTypeface(), Typeface.BOLD);
        titleView.setLayoutParams(new LayoutParams(0, LayoutParams.WRAP_CONTENT, 1f));
        titleView.setImportantForAccessibility(View.IMPORTANT_FOR_ACCESSIBILITY_NO);
        topRow.addView(titleView);

        badgeView = new TextView(context);
        badgeView.setTextSize(TypedValue.COMPLEX_UNIT_SP, 12);
        badgeView.setPadding(dp(8), dp(4), dp(8), dp(4));
        badgeView.setBackgroundColor(0x2244AA44);
        badgeView.setImportantForAccessibility(View.IMPORTANT_FOR_ACCESSIBILITY_NO);
        topRow.addView(badgeView);

        addView(topRow);

        subtitleView = new TextView(context);
        subtitleView.setTextSize(TypedValue.COMPLEX_UNIT_SP, 14);
        subtitleView.setPadding(0, dp(8), 0, 0);
        subtitleView.setImportantForAccessibility(View.IMPORTANT_FOR_ACCESSIBILITY_NO);
        addView(subtitleView);
    }

    public void configureVisibleText(String title, String subtitle, String badge) {
        titleView.setText(title);
        subtitleView.setText(subtitle);
        badgeView.setText(badge);
    }

    public void configureAccessibility(String title, String summary, String roleClassName) {
        accessibilityTitle = title;
        accessibilitySummary = summary;
        accessibilityRole = roleClassName;
    }

    @Override
    public void onInitializeAccessibilityNodeInfo(AccessibilityNodeInfo info) {
        super.onInitializeAccessibilityNodeInfo(info);
        if (accessibilityRole != null) {
            info.setClassName(accessibilityRole);
        }
        if (accessibilityTitle != null && accessibilitySummary != null) {
            info.setText(accessibilityTitle + ". " + accessibilitySummary);
            info.setContentDescription(accessibilityTitle + ". " + accessibilitySummary);
        }
        info.setClickable(true);
        info.setFocusable(true);
    }

    private int dp(int value) {
        float density = getResources().getDisplayMetrics().density;
        return Math.round(value * density);
    }
}
