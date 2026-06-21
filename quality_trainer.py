"""Argos Phase 2 - Feature Extractor + Quality Predictor.

MVP scope:
- Feature extractor: tag prefix, task_class one-hot, length, token estimates, error_sensitivity
- Quality predictor: sklearn LogisticRegression trained on dispatches.status (success vs fail proxy)
- Saves trained model to disk
- Adds /predict-quality endpoint via router reload
- Backfills predicted_quality for existing predictions

Uses existing dispatches as labeled training data.
"""
import sqlite3
import json
import os
import sys
import time
import pickle
import re
from pathlib import Path

ARGOS_DB = "/home/andy/argos/argos.db"
MODEL_PATH = "/home/andy/argos/quality_predictor.pkl"

def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)

# ============================================================
# Feature extraction
# ============================================================

# 24 task classes from Phase 0 schema
TASK_CLASSES = [
    "code_generation", "code_implementation", "code_boilerplate", "code_algorithmic",
    "debugging", "debugging_simple", "debugging_intermittent",
    "refactoring", "testing", "test_unit", "test_integration",
    "documentation", "docs_api", "docs_explainer",
    "architecture_design", "data_engineering", "devops", "security",
    "analysis", "formatting", "extraction", "creative", "classification", "conversation"
]
TASK_CLASS_INDEX = {c: i for i, c in enumerate(TASK_CLASSES)}

ERROR_SENSITIVITY_LEVELS = ["low", "medium", "high", "critical"]
ERROR_SENS_INDEX = {l: i for i, l in enumerate(ERROR_SENSITIVITY_LEVELS)}

# Tag prefix patterns -> indicators
TAG_PATTERNS = [
    ("smoke", re.compile(r'^SMOKE-|TEST-|GATE-', re.I)),
    ("audit", re.compile(r'AUDIT-|CHECK', re.I)),
    ("v2", re.compile(r'V2-|VAULT-', re.I)),
    ("wave", re.compile(r'WAVE\d-', re.I)),
    ("argos", re.compile(r'ARGOS', re.I)),
    ("backup", re.compile(r'BACKUP-|RESTORE-', re.I)),
    ("emergency", re.compile(r'CRITICAL|EMERGENCY|FIX', re.I)),
]

def extract_features(tag: str, task_class: str, error_sensitivity: str = "medium",
                     est_input_tokens: int = 1000, est_output_tokens: int = 500,
                     notes: str = "") -> list:
    """Returns a numeric feature vector. Dimension = stable across calls."""
    features = []
    
    # 1. task_class one-hot (24 dims)
    one_hot = [0.0] * len(TASK_CLASSES)
    if task_class in TASK_CLASS_INDEX:
        one_hot[TASK_CLASS_INDEX[task_class]] = 1.0
    features.extend(one_hot)
    
    # 2. error_sensitivity ordinal (1 dim)
    es = ERROR_SENS_INDEX.get(error_sensitivity, 1)
    features.append(es / 3.0)  # normalized 0..1
    
    # 3. token features (4 dims)
    features.append(min(est_input_tokens / 10000.0, 1.0))   # input scale
    features.append(min(est_output_tokens / 5000.0, 1.0))   # output scale
    features.append((est_input_tokens + est_output_tokens) / 15000.0)  # total scale
    features.append(min(len(tag) / 100.0, 1.0))  # tag length
    
    # 4. tag prefix flags (7 dims)
    for name, pat in TAG_PATTERNS:
        features.append(1.0 if pat.search(tag or "") else 0.0)
    
    # 5. notes features (3 dims)
    notes = notes or ""
    features.append(min(len(notes) / 1000.0, 1.0))    # notes length
    features.append(1.0 if "OK" in notes or "completed" in notes.lower() else 0.0)
    features.append(1.0 if "error" in notes.lower() or "failed" in notes.lower() else 0.0)
    
    return features

FEATURE_DIM = 24 + 1 + 4 + 7 + 3  # 39 dims

def feature_names() -> list:
    names = []
    names.extend([f"class_{c}" for c in TASK_CLASSES])
    names.append("error_sensitivity_ord")
    names.extend(["input_tok_norm", "output_tok_norm", "total_tok_norm", "tag_len_norm"])
    names.extend([f"tag_prefix_{n}" for n, _ in TAG_PATTERNS])
    names.extend(["notes_len_norm", "notes_has_ok", "notes_has_error"])
    return names

# ============================================================
# Training
# ============================================================

def train_quality_predictor():
    """Train a binary classifier on dispatches.status (success vs not).
    Uses sklearn LogisticRegression with held-out evaluation + 5-fold CV.
    """
    try:
        from sklearn.linear_model import LogisticRegression
        from sklearn.metrics import classification_report, confusion_matrix
        from sklearn.model_selection import train_test_split, cross_val_score, StratifiedKFold
        import numpy as np
    except ImportError:
        log("FATAL: sklearn or numpy not installed. pip install scikit-learn numpy")
        sys.exit(1)
    
    db = sqlite3.connect(ARGOS_DB, timeout=30)
    db.row_factory = sqlite3.Row
    
    # Pull labeled training data
    rows = list(db.execute("""
        SELECT dispatch_id, ts, source, provider_mode, model_used, task_class,
               actual_input_tokens, actual_output_tokens, actual_cost_usd, status
        FROM dispatches
        WHERE task_class IS NOT NULL
    """))
    
    log(f"Training set: {len(rows)} labeled dispatches")
    
    if len(rows) < 20:
        log("WARN: <20 dispatches, predictor will be unreliable but proceeding")
    
    X = []
    y = []
    for r in rows:
        # Need cost_log for tag/notes - or fall back to defaults
        # Since dispatches table doesn't store tag, we'll pull from cost_log via dispatch_id mapping
        tag = r['dispatch_id'].replace('costlog-', '') if r['dispatch_id'].startswith('costlog-') else r['dispatch_id']
        feats = extract_features(
            tag=tag,
            task_class=r['task_class'],
            error_sensitivity="medium",  # not in dispatches schema yet
            est_input_tokens=r['actual_input_tokens'] or 2000,
            est_output_tokens=r['actual_output_tokens'] or 1000,
            notes=""
        )
        X.append(feats)
        # Binary label: 1 = success, 0 = failure
        is_success = (r['status'] == 'completed')
        y.append(1 if is_success else 0)
    
    X = np.array(X)
    y = np.array(y)
    
    log(f"Class balance: success={sum(y)}, failure={len(y)-sum(y)}")
    log(f"Majority-class baseline accuracy: {max(sum(y), len(y)-sum(y))/len(y):.3f}")
    
    # 80/20 stratified split (preserves class balance, seed=42 for repro)
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.20, random_state=42, stratify=y
    )
    log(f"Train size: {len(X_train)}, Test size: {len(X_test)}")
    
    # Train on the 80%
    clf = LogisticRegression(max_iter=1000, class_weight='balanced')
    clf.fit(X_train, y_train)
    
    # Held-out test accuracy (more honest than in-sample)
    test_score = clf.score(X_test, y_test)
    log(f"Held-out test accuracy: {test_score:.3f}")
    
    # 5-fold stratified cross-validation
    try:
        cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
        cv_scores = cross_val_score(clf, X, y, cv=cv, scoring='accuracy')
        log(f"5-fold CV accuracy: {cv_scores.mean():.3f} +/- {cv_scores.std():.3f}")
        log(f"  individual folds: {[round(s, 3) for s in cv_scores.tolist()]}")
    except Exception as e:
        log(f"CV failed: {e}")
        cv_scores = np.array([test_score])
    
    # Confusion matrix on test set
    y_pred = clf.predict(X_test)
    cm = confusion_matrix(y_test, y_pred)
    log(f"Confusion matrix (test set):")
    log(f"                pred=fail  pred=success")
    log(f"  actual=fail   {cm[0][0]:>9}  {cm[0][1]:>12}")
    log(f"  actual=success {cm[1][0]:>8}  {cm[1][1]:>12}")
    
    # Final retrain on full data for the saved model (more training samples = better generalization)
    log("Refitting on full dataset for final saved model")
    clf.fit(X, y)
    train_score = clf.score(X, y)
    
    # Get top feature weights
    names = feature_names()
    weights = clf.coef_[0]
    top_features = sorted(zip(names, weights), key=lambda x: abs(x[1]), reverse=True)[:10]
    log("Top 10 feature weights:")
    for n, w in top_features:
        log(f"  {w:+.3f}  {n}")
    
    # Persist
    artifact = {
        'classifier': clf,
        'feature_dim': FEATURE_DIM,
        'feature_names': names,
        'task_classes': TASK_CLASSES,
        'trained_at': time.strftime("%Y-%m-%d %H:%M:%S"),
        'training_size': len(rows),
        'training_accuracy_full': train_score,
        'training_accuracy': float(cv_scores.mean()),  # honest metric: CV mean
        'cv_score_mean': float(cv_scores.mean()),
        'cv_score_std': float(cv_scores.std()),
        'cv_scores': cv_scores.tolist(),
        'test_accuracy_holdout': float(test_score),
        'confusion_matrix': cm.tolist(),
        'class_balance': {'success': int(sum(y)), 'failure': int(len(y) - sum(y))},
    }
    with open(MODEL_PATH, 'wb') as f:
        pickle.dump(artifact, f)
    log(f"Saved to {MODEL_PATH} ({os.path.getsize(MODEL_PATH)} bytes)")
    
    db.close()
    return artifact

# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    log("=== Phase 2: Feature Extractor + Quality Predictor ===")
    train_quality_predictor()
    log("=== done ===")
