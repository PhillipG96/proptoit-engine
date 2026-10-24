"""
Hamilton County (Chattanooga, TN) Tax History Collector
====================================================

Data Sources:
- https://www.hamiltoncountytn.gov/assessor (Property assessments)
- https://www.revenue.tn.gov/tax-situs (TNDOR tax situs)
- Hamilton County Assessor (parcel data, valuations)
- TNDOR Delinquent Tax List

Distress Flags:
- Delinquent_taxes: 2+ years behind
- Foreclosure_lien: Active lien filed
- Code_violations: Open violations
- Tax_sale_pending: Certificate on sale list
- Matrix_sale: Red-tagged property

Author: Bob Martinez
Date: 2026-05-07
"""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional
import hashlib

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hamilton_tax")

# ===========================================
# Data Models
# ===========================================

@dataclass
class PropertyAssessment:
    parcel_id: str
    assessment_year: int
    land_value: float
    improvement_value: float
    total_value: float
    owner_name: str
    address: str
    sq_ft: int
    year_built: int
    lot_acres: float
    recorded_assessment: bool = True
    assessed_change_pct: Optional[float] = None
    notes: str = ""
    previous_value_1yr: float = 0.0
    previous_value_2yr: float = 0.0
    previous_value_3yr: float = 0.0
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class RecordingEntry:
    """Recorder data - deeds, mortgages, transfers"""
    document_number: str
    document_type: str  # warranty, quitclaim, mortgage, lien
    recording_date: str
    grantor: str
    grantee: str
    property_address: str
    parcel_id: str
    consideration: float  # sale price or loan amount
    recorded_at: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class TaxDelinquency:
    """Tax status and delinquency tracking"""
    parcel_id: str
    tax_year: int
    amount_owed: float
    amount_collected: float
    penalty: float = 0.0
    interest: float = 0.0
    items: list = field(default_factory=list)
    status: str = "current"
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class DistressFlag:
    """Composite distress signal for a property"""
    parcel_id: str
    flags: dict = field(default_factory=dict)
    severity: str = "low"
    scored_at: str = field(default_factory=lambda: datetime.now().isoformat())
    
    def recalculate(self) -> str:
        active = sum(1 for v in self.flags.values() if v)
        if active >= 4:
            self.severity = "critical"
        elif active >= 3:
            self.severity = "high"
        elif active >= 2:
            self.severity = "medium"
        else:
            self.severity = "low"
        self.scored_at = datetime.now().isoformat()
        return self.severity


# ===========================================
# Distress Score Engine
# ===========================================

class DistressScorer:
    """Score properties based on multiple distress signals"""
    
    WEIGHTS = {
        "delinquent_taxes_2yr": 0.15,
        "delinquent_taxes_3yr": 0.25,
        "tax_sale_pending": 0.20,
        "foreclosure_l_filed": 0.15,
        "code_violations": 0.10,
        "owner_occupied_f": 0.10,
        "vacancy_indicator": 0.05,
        "price_drops_3yr": 0.10,
        "tax_drops_3yr": 0.15,
    }
    
    @classmethod
    def score(cls, assessment, delinquencies, recflags=None):
        recflags = recflags or {}
        flags = {}
        
        for d in delinquencies:
            if d.status in ["2y_behind", "3y_behind", "delinquent"]:
                flags[f"delinquent_{d.status}"] = True
            elif d.status == "1y_behind":
                flags["delinquent_1yr"] = True
        
        if recflags.get("tax_sale_pending", False):
            flags["tax_sale_pending"] = True
        if recflags.get("foreclosure_filed", False):
            flags["foreclosure_l_filed"] = True
        if recflags.get("code_violations", False):
            flags["code_violations"] = True
        
        if assessment.previous_value_3yr > 0:
            pct_drop = ((assessment.previous_value_3yr - assessment.total_value) / 
                       assessment.previous_value_3yr) * 100
            if pct_drop > 10:
                flags["price_drops_3yr"] = False
        
        total = sum(cls.WEIGHTS.get(k, 0.1) for k, v in flags.items() if v)
        
        flag = DistressFlag(
            parcel_id=assessment.parcel_id,
            flags=flags,
            severity="critical" if total > 0.8 else
                    "high" if total > 0.6 else
                    "medium" if total > 0.35 else
                    "low"
        )
        return flag


# ===========================================
# Zonal Distress Heat Map
# ===========================================

class ZoneHeatMap:
    """Compute distress heat by zone/block"""
    
    ZONE_LOOKUP = {
        "01": "Northside",
        "02": "East Brainerd",
        "03": "Gunbarrel",
        "04": "Signal Mountain",
        "05": "WestSide",
        "06": "Ringgold Valley",
        "07": "Southside",
        "08": "Hamilton Place",
    }
    
    @classmethod
    def compute_zone(cls, flags_list):
        """Group distress flags into zone clusters"""
        heat_map = {}
        
        # Simulate - in production uses actual parcel-to-zone mapping
        for f in flags_list:
            zone_num = hash(f.parcel_id) % 8 + 1
            zone = cls.ZONE_LOOKUP.get(str(zone_num), f"Zone{zone_num}")
            
            if zone not in heat_map:
                heat_map[zone] = {"total": 0, "distressed": 0, "flags": {}}
            
            heat_map[zone]["total"] += 1
            heat_map[zone]["distressed"] += 1 if f.severity in ["high", "critical"] else 0
            for k, v in f.flags.items():
                if v:
                    heat_map[zone]["flags"][k] = heat_map[zone]["flags"].get(k, 0) + 1
        
        # Normalize
        for zone in heat_map:
            heat_map[zone]["distress_rate"] = heat_map[zone]["distressed"] / max(heat_map[zone]["total"], 1)
        
        return heat_map


# ===========================================
# Piglit Heartbeat Monitor
# ===========================================

class HamiltonHeartbeat:
    """Piglit-based heartbeat monitor for collector health"""
    
    def __init__(self, company_id, agent_id):
        self.company_id = company_id
        self.agent_id = agent_id
        self.base_url = "http://127.0.0.1:3100/api"
        self.max_consecutive_fails = 3
        
    def check_health(self, last_collection_time, data_count):
        now = datetime.now()
        time_since = (now - last_collection_time).total_seconds()
        
        # Check the LARGER threshold first — checking >1800 before >3600
        # made 'flatline' unreachable (3600 is a subset of 1800).
        if time_since > 3600:
            heart_status = "flatline"
        elif time_since > 1800:
            heart_status = "beating_slow"
        else:
            heart_status = "beating"
            
        if data_count == 0 and heart_status == "flatline":
            data_status = "no_churn"
        elif data_count < 5 and heart_status == "beating":
            data_status = "low_churn"
        else:
            data_status = "healthy"
            
        if heart_status == "flatline" or data_status == "no_churn":
            health = "unhealthy"
        elif heart_status == "beating_slow":
            health = "warning"
        else:
            health = "healthy"
            
        return {
            "status": health,
            "heart": heart_status,
            "data": data_status,
            "last_collection": last_collection_time.isoformat(),
            "data_count": data_count,
            "time_since": int(time_since),
            "timestamp": now.isoformat()
        }
    
    def post_to_paperclip(self, result, raising="none"):
        import subprocess
        
        payload = json.dumps({**result, "raising": raising})
        try:
            r = subprocess.run(
                ["curl", "-s", "-X", "POST",
                 f"{self.base_url}/collectors/hamilton/heartbeat",
                 "-H", "Content-Type: application/json",
                 "-d", payload],
                capture_output=True, text=True, timeout=10
            )
            if r.returncode == 0:
                logger.info(f"Heartbeat posted: {result['status']}")
        except Exception as e:
            logger.warning(f"Heartbeat post failed: {e}")


# ===========================================
# Collector Main Class
# ===========================================

class HamiltonCountyCollector:
    """Main collector orchestration class"""
    
    def __init__(self, output_dir=None):
        self.data_dir = Path(output_dir) if output_dir else Path("/tmp/hamilton_collector")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        
        self.assessments = []
        self.recordings = []
        self.delinquencies = []
        self.distress_flags = []
        
        self.last_collection = datetime.now()
        self.heartbeat = HamiltonHeartbeat(
            company_id="06101ebf-37ac-49bf-8b40-7119c0c964f0",
            agent_id="3c5e927f-cf0e-4d2a-b79d-00f46a7321a6"
        )
    
    def collect_assessment(self, data):
        pa = PropertyAssessment(**data)
        
        if self.assessments:
            for existing in reversed(self.assessments):
                if existing.parcel_id == pa.parcel_id and existing.assessment_year > pa.assessment_year:
                    if existing.total_value > 0:
                        pa.assessed_change_pct = ((pa.total_value - existing.total_value) / 
                                                 existing.total_value) * 100
                    break
        
        self.assessments.append(pa)
        
        flag = DistressScorer.score(pa, self.delinquencies)
        self.distress_flags.append(flag)
        
        return pa
    
    def collect_recording(self, data):
        rec = RecordingEntry(**data)
        self.recordings.append(rec)
        return rec
    
    def collect_delinquency(self, data):
        td = TaxDelinquency(**data)
        self.delinquencies.append(td)
        return td
    
    def save_all(self):
        """Save all collected data to files"""
        results = {}
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        assess_f = self.data_dir / f"assessments_{ts}.json"
        assess_f.write_text(json.dumps([asdict(a) for a in self.assessments], indent=2))
        results["assessments"] = str(assess_f)
        
        rec_f = self.data_dir / f"recordings_{ts}.json"
        rec_f.write_text(json.dumps([asdict(r) for r in self.recordings], indent=2))
        results["recordings"] = str(rec_f)
        
        delinq_f = self.data_dir / f"delinquencies_{ts}.json"
        delinq_f.write_text(json.dumps([asdict(d) for d in self.delinquencies], indent=2))
        results["delinquencies"] = str(delinq_f)
        
        flags_f = self.data_dir / f"distress_flags_{ts}.json"
        flags_f.write_text(json.dumps(
            [{"parcel_id": f.parcel_id, "severity": f.severity, 
              "flags": f.flags, "scored_at": f.scored_at} for f in self.distress_flags],
            indent=2
        ))
        results["flags"] = str(flags_f)
        
        return results
    
    def run_health_check(self):
        result = self.heartbeat.check_health(
            self.last_collection,
            len(self.assessments) + len(self.recordings)
        )
        result["collector"] = {
            "assessments": len(self.assessments),
            "recordings": len(self.recordings),
            "delinquencies": len(self.delinquencies),
            "distress_flags": len(self.distress_flags)
        }
        result["heat_map"] = ZoneHeatMap.compute_zone(self.distress_flags)
        return result


# ===========================================
# Command Line Entry Point
# ===========================================

if __name__ == "__main__":
    import sys
    
    collector = HamiltonCountyCollector("/tmp/hamilton_collector")
    
    # Demo: collect sample data
    if len(sys.argv) > 1 and sys.argv[1] == "--demo":
        print("Running demo collection...")
        
        # Sample assessment
        collector.collect_assessment({
            "parcel_id": "HC-001-001",
            "assessment_year": 2026,
            "land_value": 85000.00,
            "improvement_value": 120000.00,
            "total_value": 205000.00,
            "owner_name": "Smith John",
            "address": "123 Main St, Chattanooga, TN 37402",
            "sq_ft": 1800,
            "year_built": 1985,
            "lot_acres": 0.25,
            "previous_value_1yr": 210000.00,
            "previous_value_2yr": 195000.00,
            "previous_value_3yr": 185000.00
        })
        
        # Sample delinquency
        collector.collect_delinquency({
            "parcel_id": "HC-001-001",
            "tax_year": 2024,
            "amount_owed": 2450.00,
            "amount_collected": 0.00,
            "penalty": 245.00,
            "interest": 73.50,
            "items": ["lot", "building"],
            "status": "2y_behind"
        })
        
        # Run health check
        health = collector.run_health_check()
        print(json.dumps(health, indent=2))
        
        # Save everything
        saved = collector.save_all()
        print(f"\nSaved files:")
        for key, path in saved.items():
            print(f"  {key}: {path}")
        
        print("\nDone!")
    else:
        # Mode: report status
        print("Hamilton County Tax Collector v1.0")
        print("Usage: python hamilton_county.py --demo")
        print("       python hamilton_county.py --status")
        
        health = collector.run_health_check()
        print(f"\nCollector Health: {health['status']}")
        print(f"  Heart: {health['heart']}")
        print(f"  Data: {health['data']}")
        print(f"  Last collection: {health['last_collection']}")
