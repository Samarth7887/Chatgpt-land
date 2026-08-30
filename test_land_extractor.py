from land_document_extractor import OCRLine, extract_land_document_from_lines


SAMPLE_LINES = [
    OCRLine(text="D.NO : 1993 / 2018", score=0.96, x_min=0, y_min=10, x_max=100, y_max=30),
    OCRLine(text="TELANGANA", score=0.99, x_min=0, y_min=35, x_max=100, y_max=55),
    OCRLine(text="Sl No. 413 Dt. 03-08-2019 Rs. 50/-", score=0.97, x_min=0, y_min=60, x_max=100, y_max=80),
    OCRLine(text="MALLURU VENU GOPAL Licensed Stamp Vendor 379230", score=0.95, x_min=0, y_min=85, x_max=100, y_max=105),
    OCRLine(text="AGREEMENT OF SALE-CUM-GENERAL POWER OF ATTORNEY", score=0.99, x_min=0, y_min=120, x_max=100, y_max=140),
    OCRLine(text="THIS AGREEMENT OF SALE-CUM GENERAL POWER OF ATTORNEY IS MADE AND EXECUTED ON THIS 05TH DAY OF AUGUST-2019", score=0.96, x_min=0, y_min=145, x_max=100, y_max=170),
    OCRLine(text="GUDURU MALAKONDAIAH, S/o. CHINNA KONDAIAH, Age.52 Years, Occup: Agriculture, R/o. H.No. 2-234/1, Main Road, Mangapet Village, Dist. Warangal, Presently Mulugu District. Aadhar No. XXXX XXXX 5888.", score=0.95, x_min=0, y_min=180, x_max=100, y_max=220),
    OCRLine(text="(HEREINAFTER CALLED THE VENDOR/PRINCIPAL)", score=0.98, x_min=0, y_min=225, x_max=100, y_max=240),
    OCRLine(text="IN FAVOUR OF", score=0.98, x_min=0, y_min=245, x_max=100, y_max=260),
    OCRLine(text="1] RANGINENI VENKATESHWAR RAO, S/o. RAMCHANDER RAO, Age: 55 Years, Occup: Business, R/o. H.No. 11-19-97, New Grain Market Road, Kashibugga, Warangal City. Aadhar No. XXXX XXXX 2146.", score=0.96, x_min=0, y_min=265, x_max=100, y_max=305),
    OCRLine(text="2] KAKKERLA SURESH, S/o. NARSAIAH, Age: 47 Years, Occup: Business, R/o. H.No. 3-13, Kommala Village, Geesugonda Mandal, Dist. Warangal. Aadhar Card No. XXXX XXXX 6636.", score=0.96, x_min=0, y_min=310, x_max=100, y_max=350),
    OCRLine(text="Contd...2/p", score=0.99, x_min=0, y_min=360, x_max=100, y_max=380),
]


def main() -> None:
    raw_text = "\n".join(line.text for line in SAMPLE_LINES)
    result = extract_land_document_from_lines(SAMPLE_LINES, raw_text, "sample_document.png")

    assert result["document_type"] == "Agreement of Sale-cum-General Power of Attorney"
    assert result["document_category"] == "Property Transaction Document"
    assert result["state"] == "Telangana"
    assert result["document_number"] == "1993/2018"
    assert result["serial_number"] == "413"
    assert result["stamp_number"] == "379230"
    assert result["stamp_value"] == "Rs.50"
    assert result["document_date"] == "03-08-2019"
    assert result["execution_date"] == "05-08-2019"
    assert len(result["parties"]) == 3
    assert result["parties"][0]["name"] == "Guduru Malakondaiah"
    assert result["parties"][0]["present_district"] == "Mulugu District"
    assert result["parties"][1]["role"] == "Vendee/Attorney"
    assert result["property"]["status"] == "CONTINUES_ON_NEXT_PAGE"
    assert result["document_features"]["multi_page_document"] is True
    assert result["document_features"]["pii_detected"] is True

    noisy_lines = [
        OCRLine(text="SCANNED 10.100:1993 2018", score=0.80, x_min=0, y_min=5, x_max=100, y_max=20),
        OCRLine(text="TELANGANA", score=0.99, x_min=0, y_min=35, x_max=100, y_max=55),
    ]
    noisy_result = extract_land_document_from_lines(noisy_lines, "\n".join(line.text for line in noisy_lines), "sample_document.png")
    assert noisy_result["document_number"] == "1993/2018"

    print("land-document extraction parser test passed")


if __name__ == "__main__":
    main()
